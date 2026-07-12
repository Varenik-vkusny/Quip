"""Тесты pure-логики speaker_switch (tight-follow с cut-переключением). ТЕСТ-ПЕРВЫМ.

Спека: docs/superpowers/specs/2026-07-13-speaker-focus-reframe-design.md.
Покрываем: build_turns (диар→реплики), fill_crop_xrange/crop_iou_x (геометрия IoU-гейта),
bind_lanes (кластер↔лицо голосованием ASD), plan_switches (greedy scheduler: min-hold,
hysteresis, IoU-гейт, wide-фолбэк, frame-snap Δ=0). I/O (диаризация/ASD) — глазами на видео.
"""

from __future__ import annotations

from app.models import Word
from app.pipeline.speaker_switch import (
    WIDE,
    SpeakerTurn,
    SwitchConfig,
    bind_lanes,
    build_turns,
    crop_iou_x,
    fill_crop_xrange,
    plan_switches,
)
from app.pipeline.stage3_reframe import SpeakerTrack


def _track(f0: int, f1: int, cx: float, *, speak_hi: tuple[int, int] | None = None) -> SpeakerTrack:
    """Дорожка со статичным cx; speak_frames = +1.0 в кадрах speak_hi=[a,b), иначе -1.0."""
    n = f1 - f0
    if speak_hi is None:
        sf: tuple[float, ...] = ()
    else:
        a, b = speak_hi
        sf = tuple(1.0 if a <= (f0 + i) < b else -1.0 for i in range(n))
    return SpeakerTrack(f0=f0, f1=f1, cx=tuple([cx] * n), width=0.12, speak=0.0, speak_frames=sf)


class TestBuildTurns:
    def _w(self, text: str, s: float, e: float, spk: int | None) -> Word:
        return Word(text=text, start=s, end=e, speaker=spk)

    def test_two_speakers_two_turns(self) -> None:
        words = [
            self._w("a", 0.0, 0.5, 0),
            self._w("b", 0.6, 1.0, 0),
            self._w("c", 1.2, 1.7, 1),
            self._w("d", 1.8, 2.2, 1),
        ]
        turns = build_turns(words, 0.0, 3.0)
        assert [(t.speaker, round(t.t_start, 2), round(t.t_end, 2)) for t in turns] == [
            (0, 0.0, 1.0),
            (1, 1.2, 2.2),
        ]
        assert turns[0].n_words == 2 and turns[1].n_words == 2

    def test_interjection_is_its_own_turn(self) -> None:
        # build_turns = сырое группирование; фильтрация коротких реплик — в plan_switches, НЕ тут.
        words = [
            self._w("x", 0.0, 1.0, 0),
            self._w("yeah", 1.1, 1.3, 1),
            self._w("y", 1.4, 2.4, 0),
        ]
        turns = build_turns(words, 0.0, 3.0)
        assert [t.speaker for t in turns] == [0, 1, 0]

    def test_none_speaker_inherits_current(self) -> None:
        words = [
            self._w("a", 0.0, 0.5, 0),
            self._w("um", 0.6, 0.8, None),
            self._w("b", 0.9, 1.3, 0),
        ]
        turns = build_turns(words, 0.0, 2.0)
        assert len(turns) == 1 and turns[0].speaker == 0 and turns[0].n_words == 3

    def test_consecutive_same_speaker_one_turn(self) -> None:
        # Один спикер держит слово, даже с паузами внутри — турн кончается ТОЛЬКО на смене спикера.
        words = [
            self._w("a", 0.0, 0.5, 0),
            self._w("b", 0.7, 1.2, 0),  # пауза 0.2с — тот же спикер → один турн
            self._w("c", 2.0, 2.5, 0),  # пауза 0.8с — всё ещё его слово → тот же турн
        ]
        turns = build_turns(words, 0.0, 3.0)
        assert len(turns) == 1 and round(turns[0].t_end, 2) == 2.5 and turns[0].n_words == 3

    def test_empty_or_no_labels(self) -> None:
        assert build_turns([], 0.0, 3.0) == []
        allnone = [self._w("a", 0.0, 0.5, None), self._w("b", 0.6, 1.0, None)]
        assert build_turns(allnone, 0.0, 2.0) == []

    def test_times_are_clip_relative(self) -> None:
        words = [self._w("a", 10.0, 10.5, 0), self._w("b", 10.6, 11.0, 1)]
        turns = build_turns(words, 10.0, 12.0)
        assert round(turns[0].t_start, 2) == 0.0 and round(turns[1].t_start, 2) == 0.6


class TestFillCropXrange:
    def test_centered(self) -> None:
        lo, hi = fill_crop_xrange(0.5, 0.316)
        assert abs(lo - 0.342) < 1e-3 and abs(hi - 0.658) < 1e-3

    def test_clamps_left(self) -> None:
        lo, hi = fill_crop_xrange(0.05, 0.316)
        assert lo == 0.0 and abs(hi - 0.316) < 1e-6

    def test_clamps_right(self) -> None:
        lo, hi = fill_crop_xrange(0.98, 0.316)
        assert abs(lo - (1.0 - 0.316)) < 1e-6 and hi == 1.0


class TestCropIouX:
    def test_disjoint_is_zero(self) -> None:
        assert crop_iou_x((0.06, 0.38), (0.49, 0.81)) == 0.0

    def test_identical_is_one(self) -> None:
        assert abs(crop_iou_x((0.1, 0.4), (0.1, 0.4)) - 1.0) < 1e-9

    def test_half_overlap(self) -> None:
        # a=[0,2] b=[1,3] inter=1 union=3 → 1/3
        assert abs(crop_iou_x((0.0, 0.2), (0.1, 0.3)) - (1.0 / 3.0)) < 1e-6


class TestBindLanes:
    def test_two_lanes_bound_to_right_speaker(self) -> None:
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(1, 3.0, 6.0, 10)]
        # fps=30: turn0 frames 0..90, turn1 90..180
        lane0 = _track(0, 180, 0.2, speak_hi=(0, 90))  # говорит в turn0
        lane1 = _track(0, 180, 0.65, speak_hi=(90, 180))  # говорит в turn1
        assert bind_lanes(turns, [lane0, lane1], 30.0) == {0: 0, 1: 1}

    def test_one_noisy_turn_does_not_flip_majority(self) -> None:
        # spk0 в 3 турнах; лейн0 говорит в 2, лейн1 (шумный ASD) в 1 → majority = лейн0 (2/3 ≥ 0.6).
        turns = [
            SpeakerTurn(0, 0.0, 1.0, 3),
            SpeakerTurn(0, 1.0, 2.0, 3),
            SpeakerTurn(0, 2.0, 3.0, 3),
        ]
        lane0 = _track(0, 90, 0.2, speak_hi=(0, 60))  # говорит в turn0+turn1 → 2 голоса
        lane1 = _track(0, 90, 0.65, speak_hi=(60, 90))  # говорит в turn2 → 1 голос
        assert bind_lanes(turns, [lane0, lane1], 30.0)[0] == 0

    def test_offscreen_cluster_is_none(self) -> None:
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(5, 3.0, 6.0, 10)]
        lane0 = _track(0, 180, 0.2, speak_hi=(0, 90))
        lane1 = _track(0, 180, 0.65, speak_hi=(0, 90))  # никто не говорит в turn1 (spk5 off-screen)
        b = bind_lanes(turns, [lane0, lane1], 30.0)
        assert b.get(5) is None

    def test_ambiguous_below_conf_is_none(self) -> None:
        # spk0 два турна: лицо0 выигрывает один, лицо1 другой → 1:1 → conf 0.5 < 0.6 → None
        turns = [SpeakerTurn(0, 0.0, 1.0, 3), SpeakerTurn(0, 1.0, 2.0, 3)]
        lane0 = _track(0, 60, 0.2, speak_hi=(0, 30))
        lane1 = _track(0, 60, 0.65, speak_hi=(30, 60))
        assert bind_lanes(turns, [lane0, lane1], 30.0, conf_min=0.6).get(0) is None


class TestPlanSwitches:
    def _cfg(self, **kw: float) -> SwitchConfig:
        base = dict(min_hold_sec=1.7, iou_alpha=0.15, iou_beta=0.30, establish_sec=0.0)
        base.update(kw)
        return SwitchConfig(**base)  # type: ignore[arg-type]

    def test_clean_two_speaker_cut(self) -> None:
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(1, 3.0, 6.0, 10)]
        tracks = [_track(0, 180, 0.2), _track(0, 180, 0.65)]
        regs = plan_switches(turns, {0: 0, 1: 1}, tracks, 0.316, 0.0, 6.0, 30.0, self._cfg())
        assert [(round(r.t0, 3), round(r.t1, 3), r.target) for r in regs] == [
            (0.0, 3.0, 0),
            (3.0, 6.0, 1),
        ]

    def test_interjection_lingers(self) -> None:
        turns = [
            SpeakerTurn(0, 0.0, 3.0, 10),
            SpeakerTurn(1, 3.0, 3.4, 1),  # 0.4s < min_hold → skip
            SpeakerTurn(0, 3.4, 6.0, 10),
        ]
        tracks = [_track(0, 180, 0.2), _track(0, 180, 0.65)]
        regs = plan_switches(turns, {0: 0, 1: 1}, tracks, 0.316, 0.0, 6.0, 30.0, self._cfg())
        assert len(regs) == 1 and regs[0].target == 0

    def test_close_speakers_high_iou_go_wide(self) -> None:
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(1, 3.0, 6.0, 10)]
        tracks = [_track(0, 180, 0.40), _track(0, 180, 0.46)]  # почти одинаковый кроп → IoU высок
        regs = plan_switches(turns, {0: 0, 1: 1}, tracks, 0.316, 0.0, 6.0, 30.0, self._cfg())
        assert regs[0].target == 0 and regs[-1].target == WIDE

    def test_unbound_cluster_goes_wide(self) -> None:
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(1, 3.0, 6.0, 10)]
        tracks = [_track(0, 180, 0.2), _track(0, 180, 0.65)]
        regs = plan_switches(turns, {0: 0, 1: None}, tracks, 0.316, 0.0, 6.0, 30.0, self._cfg())
        assert regs[0].target == 0 and regs[-1].target == WIDE

    def test_late_switch_lingers_to_end(self) -> None:
        # Смена спикера в последние < min_hold секунды шота → не режем (короткий хвост не нужен).
        # Длинная реплика (проходит lookahead), но старт в последние <min_hold с → tail-guard.
        turns = [SpeakerTurn(0, 0.0, 6.5, 20), SpeakerTurn(1, 6.5, 8.5, 8)]
        tracks = [_track(0, 240, 0.2), _track(0, 240, 0.65)]
        regs = plan_switches(turns, {0: 0, 1: 1}, tracks, 0.316, 0.0, 8.0, 30.0, self._cfg())
        assert len(regs) == 1 and regs[0].target == 0

    def test_boundaries_frame_snapped_dropframe_fps(self) -> None:
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(1, 3.0, 6.0, 10)]
        tracks = [_track(0, 180, 0.2), _track(0, 180, 0.65)]
        for fps in (24.0, 25.0, 29.97, 30.0, 60.0):
            regs = plan_switches(turns, {0: 0, 1: 1}, tracks, 0.316, 0.0, 6.0, fps, self._cfg())
            for r in regs:
                assert abs(r.t0 * fps - round(r.t0 * fps)) < 1e-6
                assert abs(r.t1 * fps - round(r.t1 * fps)) < 1e-6

    def test_regions_contiguous_and_cover_shot(self) -> None:
        turns = [
            SpeakerTurn(0, 0.0, 2.0, 8),
            SpeakerTurn(1, 2.0, 4.0, 8),
            SpeakerTurn(0, 4.0, 6.0, 8),
        ]
        tracks = [_track(0, 180, 0.2), _track(0, 180, 0.65)]
        regs = plan_switches(turns, {0: 0, 1: 1}, tracks, 0.316, 0.0, 6.0, 30.0, self._cfg())
        assert regs[0].t0 == 0.0 and regs[-1].t1 == 6.0
        for a, b in zip(regs, regs[1:], strict=False):  # offset-пары: длины намеренно N и N-1
            assert a.t1 == b.t0
