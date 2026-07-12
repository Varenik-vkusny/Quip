"""Удаление пауз — ядро (pure, алгоритм v3), тест-первым.

Спека: docs/superpowers/specs/2026-07-12-pause-removal-design.md §5.
Паузы = гэпы между словами ∈ [min_pause, max_pause]; вырезаем с АСИММЕТРИЧНЫМ отступом (lead_out
после слова > lead_in перед след. словом), границы FRAME-SNAPPED на native-fps грид (Δ=0, инвариант
REFRAME_FPS). Смена говорящего → НЕ режем. Защита краёв клипа. apply_silence вычитает дырки из
source_intervals (reuse ops._subtract_range); disabled → строгий no-op.
"""

import pytest

from app.editor.silence import apply_silence, compute_silence_cuts
from app.models import SilenceConfig, SourceInterval, Word

FPS = 25.0  # кадр = 0.04с


def W(text: str, s: float, e: float, speaker: int | None = None) -> Word:
    return Word(text=text, start=s, end=e, speaker=speaker)


def IV(s: float, e: float) -> SourceInterval:
    return SourceInterval(source_start=s, source_end=e)


CFG = SilenceConfig(enabled=True)  # v3-дефолты: min 350, lead_in 50, lead_out 150, boundary 300


def _on_grid(t: float) -> bool:
    return abs(t * FPS - round(t * FPS)) < 1e-6


class TestComputeSilenceCuts:
    def test_cuts_gap_in_range(self) -> None:
        # gap 0.6с ∈ [0.35, 2.0] между «b»(end 2.0) и «c»(start 2.6) → одна дырка
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.6)]
        cuts = compute_silence_cuts(words, [IV(0.0, 10.0)], CFG, fps=FPS)
        assert len(cuts) == 1
        cs, ce = cuts[0]
        assert 2.0 <= cs < ce <= 2.6  # дырка ВНУТРИ гэпа, поджата отступами
        assert _on_grid(cs) and _on_grid(ce)

    def test_keeps_gap_below_min(self) -> None:
        words = [W("a", 0.0, 1.0), W("b", 1.3, 2.3)]  # gap 0.3 < 0.35 → ритм, не режем
        assert compute_silence_cuts(words, [IV(0.0, 5.0)], CFG, fps=FPS) == []

    def test_keeps_gap_above_max(self) -> None:
        words = [W("a", 0.0, 1.0), W("b", 4.0, 5.0)]  # gap 3.0 > 2.0 → драма, не режем
        assert compute_silence_cuts(words, [IV(0.0, 6.0)], CFG, fps=FPS) == []

    def test_no_words(self) -> None:
        assert compute_silence_cuts([], [IV(0.0, 5.0)], CFG, fps=FPS) == []

    def test_overlapping_timings_not_cut(self) -> None:
        words = [W("a", 0.0, 2.0), W("b", 1.5, 3.0)]  # gap = -0.5 (ASR overlap) → не режем
        assert compute_silence_cuts(words, [IV(0.0, 5.0)], CFG, fps=FPS) == []

    def test_gap_outside_intervals_not_cut(self) -> None:
        # гэп ∈ диапазоне, но НЕ внутри kept-интервала (уже вырезан ручным тримом) → не режем
        words = [W("a", 0.0, 1.0), W("b", 1.6, 2.6)]  # gap 0.6, но интервал только [0,1.0]
        assert compute_silence_cuts(words, [IV(0.0, 1.0)], CFG, fps=FPS) == []

    def test_padding_consumes_gap_no_cut(self) -> None:
        # lead_out 1000мс + lead_in 50мс > gap 0.45 → рез вырожден → не режем (avoid harsh cuts)
        cfg = SilenceConfig(enabled=True, min_pause_ms=400, lead_out_ms=1000)
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.45, 3.45)]  # gap 0.45
        assert compute_silence_cuts(words, [IV(0.0, 5.0)], cfg, fps=FPS) == []


class TestAsymmetricPadding:
    """v3: хвост слова «течёт» (звенит) → lead_out больше; онсет след. слова резкий → lead_in мал.
    На конце предложения lead_out ещё больше (вдох/ритм — сигнал парсинга)."""

    def test_leaves_more_after_word_than_before_next(self) -> None:
        words = [W("a", 0.0, 1.0), W("mid", 1.0, 2.0), W("x", 2.6, 3.6)]  # gap [2.0,2.6]
        cs, ce = compute_silence_cuts(words, [IV(0.0, 10.0)], CFG, fps=FPS)[0]
        kept_after = cs - 2.0  # оставлено ПОСЛЕ «mid» (lead_out 150мс)
        kept_before = 2.6 - ce  # оставлено ПЕРЕД «x» (lead_in 50мс)
        assert kept_after > kept_before

    def test_sentence_end_keeps_more_than_within(self) -> None:
        wb = [W("a", 0.0, 1.0), W("end.", 1.0, 2.0), W("x", 2.6, 3.6)]  # конец предложения
        ww = [W("a", 0.0, 1.0), W("mid", 1.0, 2.0), W("x", 2.6, 3.6)]  # внутри мысли
        cb = compute_silence_cuts(wb, [IV(0.0, 10.0)], CFG, fps=FPS)[0]
        cw = compute_silence_cuts(ww, [IV(0.0, 10.0)], CFG, fps=FPS)[0]
        assert (cb[1] - cb[0]) < (cw[1] - cw[0])  # граница режет МЕНЬШЕ → оставляет больше вдоха


class TestSpeakerChange:
    """v3: гэп через смену говорящего = турн-тейкинг (осмыслен + тайминг ASR плывёт) → НЕ режем."""

    def test_speaker_change_not_cut(self) -> None:
        w = [W("a", 0.0, 1.0, 0), W("b", 1.0, 2.0, 0), W("c", 2.6, 3.6, 1)]  # gap b→c: смена
        assert compute_silence_cuts(w, [IV(0.0, 10.0)], CFG, fps=FPS) == []

    def test_same_speaker_cut(self) -> None:
        w = [W("a", 0.0, 1.0, 0), W("b", 1.0, 2.0, 0), W("c", 2.6, 3.6, 0)]  # один спикер
        assert len(compute_silence_cuts(w, [IV(0.0, 10.0)], CFG, fps=FPS)) == 1


class TestClipEdgeProtection:
    """v3: не режем впритык к началу/концу клипа (резкий старт / обрыв последнего слова)."""

    def test_no_cut_near_clip_start(self) -> None:
        words = [W("a", 0.0, 0.1), W("b", 0.6, 1.6)]  # gap [0.1,0.6]=0.5, но у самого старта клипа
        assert compute_silence_cuts(words, [IV(0.0, 10.0)], CFG, fps=FPS) == []

    def test_no_cut_near_clip_end(self) -> None:
        words = [W("a", 8.0, 9.0), W("b", 9.7, 9.95)]  # gap [9.0,9.7], рез упрётся в конец клипа
        assert compute_silence_cuts(words, [IV(0.0, 10.0)], CFG, fps=FPS) == []


class TestApplySilence:
    def test_disabled_is_strict_noop(self) -> None:
        words = [W("a", 0.0, 1.0), W("b", 2.6, 3.6)]
        ivs = [IV(0.0, 10.0)]
        assert apply_silence(ivs, words, SilenceConfig(enabled=False), fps=FPS) == ivs

    def test_splits_interval_on_cut(self) -> None:
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.6)]  # gap 0.6
        out = apply_silence([IV(0.0, 10.0)], words, CFG, fps=FPS)
        assert len(out) == 2
        assert out[0].source_start == pytest.approx(0.0)
        assert out[1].source_end == pytest.approx(10.0)
        assert out[0].source_end < out[1].source_start  # дырка существует

    def test_tight_shorter_than_original(self) -> None:
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.6)]
        out = apply_silence([IV(0.0, 10.0)], words, CFG, fps=FPS)
        total = sum(iv.source_end - iv.source_start for iv in out)
        assert total < 10.0


class TestComposeMultiInterval:
    def test_composes_with_prior_trim_multi_interval(self) -> None:
        # вход уже мульти-интервальный (после ручного трима [3.0,5.0]); тишина режет ВНУТРИ первого
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.0)]  # gap b→c = 0.6
        ivs = [IV(0.0, 3.0), IV(5.0, 10.0)]
        out = apply_silence(ivs, words, CFG, fps=FPS)
        assert len(out) == 3  # [0,~2.16],[~2.56,3.0],[5,10]
        assert out[-1] == IV(5.0, 10.0)  # второй интервал нетронут
