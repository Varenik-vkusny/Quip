"""Удаление пауз — ядро (pure), тест-первым.

Спека: docs/superpowers/specs/2026-07-12-pause-removal-design.md §5.
Паузы = гэпы между словами ∈ [min_pause, max_pause]; вырезаем с padding по краям, границы
FRAME-SNAPPED на native-fps грид (round(t*fps)/fps — инвариант REFRAME_FPS, Δ=0). apply_silence
вычитает дырки из source_intervals (reuse ops._subtract_range); disabled → строгий no-op.
"""

import pytest

from app.editor.silence import apply_silence, compute_silence_cuts
from app.models import SilenceConfig, SourceInterval, Word

FPS = 25.0  # кадр = 0.04с


def W(text: str, s: float, e: float) -> Word:
    return Word(text=text, start=s, end=e)


def IV(s: float, e: float) -> SourceInterval:
    return SourceInterval(source_start=s, source_end=e)


CFG = SilenceConfig(enabled=True, min_pause_ms=400, max_pause_ms=2000, padding_ms=50)


def _on_grid(t: float) -> bool:
    return abs(t * FPS - round(t * FPS)) < 1e-6


class TestComputeSilenceCuts:
    def test_cuts_gap_in_range(self) -> None:
        # gap 0.6с ∈ [0.4, 2.0] между «b»(end 2.0) и «c»(start 2.6) → одна дырка
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.6)]
        cuts = compute_silence_cuts(words, [IV(0.0, 10.0)], CFG, fps=FPS)
        assert len(cuts) == 1
        cs, ce = cuts[0]
        # дырка ВНУТРИ гэпа [2.0, 2.6], поджата padding'ом, границы на кадровом гриде
        assert 2.0 <= cs < ce <= 2.6
        assert _on_grid(cs) and _on_grid(ce)

    def test_keeps_gap_below_min(self) -> None:
        words = [W("a", 0.0, 1.0), W("b", 1.3, 2.3)]  # gap 0.3 < 0.4 → ритм, не режем
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

    def test_keep_consumes_gap_no_inverted_cut(self) -> None:
        # within_keep 1000мс (500 на край) > gap 0.45 → остаток съедает гэп → не режем
        cfg = SilenceConfig(enabled=True, min_pause_ms=400, within_keep_ms=1000)
        words = [W("a", 0.0, 1.0), W("b", 1.45, 2.45)]  # gap 0.45 ∈ [0.4, 2.0], within
        assert compute_silence_cuts(words, [IV(0.0, 5.0)], cfg, fps=FPS) == []


class TestBoundaryAware:
    """Слой A: на ГРАНИЦЕ мысли (конец предложения / длинный гэп / смена спикера) остаётся больший
    вдох, чем ВНУТРИ мысли (наука: граничная пауза = сигнал парсинга, убирать нельзя)."""

    def test_sentence_end_keeps_more_than_within(self) -> None:
        wb = [W("a", 0.0, 1.0), W("end.", 1.0, 2.0), W("x", 2.6, 3.6)]  # gap [2.0,2.6] граница
        ww = [W("a", 0.0, 1.0), W("mid", 1.0, 2.0), W("x", 2.6, 3.6)]  # gap [2.0,2.6] внутри
        cb = compute_silence_cuts(wb, [IV(0.0, 10.0)], CFG, fps=FPS)[0]
        cw = compute_silence_cuts(ww, [IV(0.0, 10.0)], CFG, fps=FPS)[0]
        assert (cb[1] - cb[0]) < (cw[1] - cw[0])  # граница режет МЕНЬШЕ → оставляет больше вдоха

    def test_long_gap_is_boundary(self) -> None:
        # длинный гэп (1.5с) сам граница даже без пунктуации → остаток ~boundary_keep (220мс)
        ww = [W("a", 0.0, 1.0), W("mid", 1.0, 2.0), W("x", 3.5, 4.5)]  # gap [2.0,3.5]=1.5s >0.7
        c = compute_silence_cuts(ww, [IV(0.0, 10.0)], CFG, fps=FPS)[0]
        assert 1.5 - (c[1] - c[0]) == pytest.approx(0.22, abs=0.06)

    def test_speaker_change_is_boundary(self) -> None:
        w = [
            Word(text="a", start=0.0, end=1.0, speaker=0),
            Word(text="b", start=1.0, end=2.0, speaker=0),
            Word(text="c", start=2.6, end=3.6, speaker=1),  # gap b→c: смена спикера = граница
        ]
        c = compute_silence_cuts(w, [IV(0.0, 10.0)], CFG, fps=FPS)[0]
        assert (2.6 - 2.0) - (c[1] - c[0]) > 0.15  # оставлен большой вдох (>within)


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


class TestAudioRefinement:
    """Гибрид (ресёрч 2026-07-12): word-gap решает ЧТО резать, `silence_regions` (ffmpeg
    silencedetect) уточняет ГДЕ — режем только аудио-подтверждённую тишину, точные границы."""

    def test_refines_cut_to_confirmed_silence(self) -> None:
        # гэп [2.0, 2.6], аудио-тишина только [2.10, 2.50] → рез ВНУТРИ неё, не по гэпу
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.6)]
        cuts = compute_silence_cuts(
            words, [IV(0.0, 10.0)], CFG, fps=FPS, silence_regions=[(2.10, 2.50)]
        )
        assert len(cuts) == 1
        cs, ce = cuts[0]
        assert 2.10 <= cs < ce <= 2.50  # рез строго внутри подтверждённой тишины
        assert _on_grid(cs) and _on_grid(ce)

    def test_skips_gap_with_no_confirmed_silence(self) -> None:
        # word-gap ∈ [min,max], но аудио НЕ тихое (тихая речь/вздох) → ложное срабатывание, не режем
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.6)]
        cuts = compute_silence_cuts(words, [IV(0.0, 10.0)], CFG, fps=FPS, silence_regions=[])
        assert cuts == []

    def test_none_regions_falls_back_to_wordgap(self) -> None:
        # silence_regions=None → прежнее word-gap поведение (обратная совместимость)
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.6)]
        cuts = compute_silence_cuts(words, [IV(0.0, 10.0)], CFG, fps=FPS, silence_regions=None)
        assert len(cuts) == 1
        assert 2.0 <= cuts[0][0] < cuts[0][1] <= 2.6


class TestComposeMultiInterval:
    def test_composes_with_prior_trim_multi_interval(self) -> None:
        # вход уже мульти-интервальный (после ручного трима [3.0,5.0]); тишина режет ВНУТРИ первого
        words = [W("a", 0.0, 1.0), W("b", 1.0, 2.0), W("c", 2.6, 3.0)]  # gap b→c = 0.6
        ivs = [IV(0.0, 3.0), IV(5.0, 10.0)]
        out = apply_silence(ivs, words, CFG, fps=FPS)
        assert len(out) == 3  # [0,~2.04],[~2.56,3.0],[5,10]
        assert out[-1] == IV(5.0, 10.0)  # второй интервал нетронут
