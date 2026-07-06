"""Stage 2b (B1) — Gemini-vision window review. PURE-logic tests, тест-первым.

Баг-опасные места (план Phase B): парсинг ответа модели → VisionReview, кламп
модель-предложенных краёв к ±1 слову детерминированной границы (защита от дрейфа
таймкодов модели), ре-ранк по мультимодальному score. Сеть НЕ трогаем — мокаем
только через готовый VisionReview (клиент Gemini здесь не создаётся).
"""

import pytest

from app.errors import JobError
from app.models import ClipType, Segment, Word
from app.pipeline.stage2b_vision_review import (
    VisionReview,
    apply_review,
    apply_vision_reviews,
    clamp_edges_to_words,
    neutral_review,
    parse_vision_response,
    rerank_and_cap,
)


# индекс k → слово [k, k+0.8]; "Sn." заканчивает предложение (как в test_stage2_select)
def uniform(n: int) -> list[Word]:
    return [Word(text=f"w{k}", start=float(k), end=float(k) + 0.8) for k in range(n)]


def mkseg(start: float, end: float, score: float = 0.5, typ: str = "hook") -> Segment:
    return Segment(start=start, end=end, reason="r", score=score, type=ClipType(typ))


# ─────────────────────────── parse_vision_response ───────────────────────────


class TestParseVisionResponse:
    def test_full_payload(self) -> None:
        text = (
            '{"hook_strength": 0.82, "hook_reason": "strong claim + face", '
            '"is_complete_thought": true, "edge_issue_start": false, '
            '"edge_issue_end": true, "trimmed_start_sec": null, '
            '"trimmed_end_sec": 27.4, "subject_hint": "left speaker", '
            '"fill_vs_fit_hint": "fill"}'
        )
        r = parse_vision_response(text)
        assert r.ok is True
        assert r.hook_strength == 0.82
        assert r.hook_reason == "strong claim + face"
        assert r.is_complete_thought is True
        assert r.edge_issue_end is True
        assert r.trimmed_start_sec is None
        assert r.trimmed_end_sec == 27.4
        assert r.subject_hint == "left speaker"
        assert r.fill_vs_fit_hint == "fill"

    def test_hook_strength_clamped_above_one(self) -> None:
        r = parse_vision_response('{"hook_strength": 1.7, "hook_reason": "x"}')
        assert r.hook_strength == 1.0

    def test_hook_strength_clamped_below_zero(self) -> None:
        r = parse_vision_response('{"hook_strength": -0.5, "hook_reason": "x"}')
        assert r.hook_strength == 0.0

    def test_optional_fields_default_to_none(self) -> None:
        r = parse_vision_response('{"hook_strength": 0.4, "hook_reason": "x"}')
        assert r.trimmed_start_sec is None
        assert r.trimmed_end_sec is None
        assert r.subject_hint is None
        assert r.fill_vs_fit_hint is None
        assert r.is_complete_thought is True  # neutral default when absent

    def test_non_json_raises(self) -> None:
        with pytest.raises(JobError):
            parse_vision_response("not json at all {")

    def test_missing_hook_strength_raises(self) -> None:
        # hook_strength — ядро сигнала; его отсутствие = сбой парса (→ neutral upstream), а не 0.0
        with pytest.raises(JobError):
            parse_vision_response('{"hook_reason": "x"}')


# ─────────────────────────── clamp_edges_to_words (drift guard) ───────────────────────────


class TestClampEdgesToWords:
    def test_none_keeps_deterministic_edges(self) -> None:
        w = uniform(20)
        s, e = clamp_edges_to_words(w, 5.0, 10.8, None, None)
        assert (s, e) == (5.0, 10.8)

    def test_start_snaps_one_word_back(self) -> None:
        # старт возле слова 4 (start=4.0) при детерминированном слове 5 → на 1 слово назад
        w = uniform(20)
        s, e = clamp_edges_to_words(w, 5.0, 10.8, 4.1, None)
        assert s == 4.0
        assert e == 10.8

    def test_start_drift_clamped_to_one_word(self) -> None:
        # модель уехала далеко назад (-100) → всё равно не дальше 1 слова (word4.start=4.0)
        w = uniform(20)
        s, _ = clamp_edges_to_words(w, 5.0, 10.8, -100.0, None)
        assert s == 4.0

    def test_start_no_change_preserves_determinism(self) -> None:
        # предложение почти совпадает с детерминированным словом 5 → возвращаем ИСХОДНЫЙ seg_start
        w = uniform(20)
        s, _ = clamp_edges_to_words(w, 5.0, 10.8, 5.03, None)
        assert s == 5.0

    def test_end_extends_one_word(self) -> None:
        # предложенный конец возле слова 11 (end=11.8) при детерминированном слове 10 → +1 слово
        w = uniform(20)
        _, e = clamp_edges_to_words(w, 5.0, 10.8, None, 11.9)
        assert e == 11.8

    def test_end_drift_clamped_to_one_word(self) -> None:
        w = uniform(20)
        _, e = clamp_edges_to_words(w, 5.0, 10.8, None, 999.0)
        assert e == 11.8  # word11.end, не дальше 1 слова вперёд

    def test_inversion_guard(self) -> None:
        # патологическое предложение (старт после конца) → откат к детерминированным границам
        w = uniform(20)
        s, e = clamp_edges_to_words(w, 5.0, 6.0, 5.9, 4.9, max_word_shift=3)
        assert s == 5.0 and e == 6.0

    def test_empty_words_returns_deterministic(self) -> None:
        s, e = clamp_edges_to_words([], 5.0, 10.0, 3.0, 12.0)
        assert (s, e) == (5.0, 10.0)


# ─────────────────────────── apply_review (one segment) ───────────────────────────


class TestApplyReview:
    def test_neutral_review_is_identity(self) -> None:
        w = uniform(20)
        seg = mkseg(5.0, 10.8, score=0.4)
        out = apply_review(seg, neutral_review(), w)
        assert out.score == 0.4
        assert (out.start, out.end) == (5.0, 10.8)

    def test_ok_review_overrides_score(self) -> None:
        w = uniform(20)
        seg = mkseg(5.0, 10.8, score=0.4)
        rev = VisionReview(
            hook_strength=0.9, hook_reason="x", is_complete_thought=True,
            edge_issue_start=False, edge_issue_end=False,
            trimmed_start_sec=None, trimmed_end_sec=None,
            subject_hint=None, fill_vs_fit_hint=None, ok=True,
        )  # fmt: skip
        out = apply_review(seg, rev, w)
        assert out.score == 0.9
        assert (out.start, out.end) == (5.0, 10.8)  # no trims → edges unchanged

    def test_ok_review_applies_window_relative_trim(self) -> None:
        # окно = [seg.start, seg.end]; trimmed_*_sec ОТНОСИТЕЛЬНЫ старта окна (0 = seg.start).
        # seg 5.0..10.8; trimmed_start_sec=-0.9 → abs 4.1 → снап на 1 слово назад (word4.start=4.0)
        w = uniform(20)
        seg = mkseg(5.0, 10.8, score=0.4)
        rev = VisionReview(
            hook_strength=0.7, hook_reason="x", is_complete_thought=True,
            edge_issue_start=True, edge_issue_end=False,
            trimmed_start_sec=-0.9, trimmed_end_sec=None,
            subject_hint=None, fill_vs_fit_hint=None, ok=True,
        )  # fmt: skip
        out = apply_review(seg, rev, w)
        assert out.start == 4.0
        assert out.end == 10.8


# ─────────────────────────── rerank_and_cap ───────────────────────────


class TestRerankAndCap:
    def test_sorts_desc_and_caps(self) -> None:
        segs = [mkseg(0, 20, 0.3), mkseg(30, 50, 0.9), mkseg(60, 80, 0.6)]
        out = rerank_and_cap(segs, max_clips=2)
        assert [s.score for s in out] == [0.9, 0.6]

    def test_cap_larger_than_len_keeps_all_sorted(self) -> None:
        segs = [mkseg(0, 20, 0.3), mkseg(30, 50, 0.9)]
        out = rerank_and_cap(segs, max_clips=10)
        assert [s.score for s in out] == [0.9, 0.3]

    def test_stable_on_ties(self) -> None:
        a, b = mkseg(0, 20, 0.5), mkseg(30, 50, 0.5)
        out = rerank_and_cap([a, b], max_clips=2)
        assert out[0] is a and out[1] is b


# ─────────────────── apply_vision_reviews (integration, no network) ───────────────────


class TestApplyVisionReviews:
    def test_reorders_by_multimodal_score(self) -> None:
        w = uniform(120)
        segs = [mkseg(0, 20, 0.8), mkseg(30, 50, 0.2)]
        reviews = [
            VisionReview(0.1, "weak", True, False, False, None, None, None, None, ok=True),
            VisionReview(0.95, "viral", True, False, False, None, None, None, None, ok=True),
        ]
        out = apply_vision_reviews(segs, reviews, w, max_clips=2)
        # второй сегмент получил мультимодальный 0.95 → он теперь первый
        assert out[0].start == 30.0 and out[0].score == 0.95
        assert out[1].start == 0.0 and out[1].score == 0.1

    def test_neutral_reviews_keep_deterministic(self) -> None:
        w = uniform(120)
        segs = [mkseg(0, 20, 0.8), mkseg(30, 50, 0.2)]
        reviews = [neutral_review(), neutral_review()]
        out = apply_vision_reviews(segs, reviews, w, max_clips=2)
        assert [s.score for s in out] == [0.8, 0.2]  # unchanged order (already desc)
