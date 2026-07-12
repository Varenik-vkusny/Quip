"""Тесты pure-математики Stage 3 (reframe 9:16) — тест-первым.

Cut-aligned reframe: TrackPoint/TrackRegion, classify_frame, smooth_centers,
merge_short_regions, build_regions_from_shots, plan_regions, build_shots_frames.
I/O (ffmpeg/MediaPipe) тестируем глазами на сэмпле.
"""

from pathlib import Path

import pytest

from app.errors import JobError
from app.pipeline.stage3_reframe import (
    TrackPoint,
    TrackRegion,
    classify_frame,
    compute_crop_window,
    merge_short_regions,
    shot_is_wide,
    smooth_centers,
)


class TestResampleTrackSpeakFrames:
    """resample_track тянет per-frame ASD (speak_frames) в нативную сетку вместе с cx (Δ=0)."""

    def test_speak_frames_resampled_with_cx(self) -> None:
        from app.pipeline.stage3_reframe import SpeakerTrack, resample_track

        t = SpeakerTrack(
            f0=0,
            f1=10,
            cx=tuple(i / 10 for i in range(10)),
            width=0.1,
            speak=4.5,
            speak_frames=tuple(float(i) for i in range(10)),
        )
        r = resample_track(t, 25.0, 30.0)
        assert len(r.speak_frames) == len(r.cx) == r.f1 - r.f0
        assert r.speak_frames[0] == 0.0 and r.speak_frames[-1] == 9.0

    def test_empty_speak_frames_stays_empty(self) -> None:
        from app.pipeline.stage3_reframe import SpeakerTrack, resample_track

        t = SpeakerTrack(f0=0, f1=10, cx=tuple([0.5] * 10), width=0.1, speak=-9.0)
        assert resample_track(t, 25.0, 30.0).speak_frames == ()

    def test_no_resample_is_identity(self) -> None:
        from app.pipeline.stage3_reframe import SpeakerTrack, resample_track

        t = SpeakerTrack(
            f0=0, f1=5, cx=(0.1, 0.2, 0.3, 0.4, 0.5), width=0.1, speak=0.0,
            speak_frames=(1.0, 2.0, 3.0, 4.0, 5.0),
        )  # fmt: skip
        assert resample_track(t, 25.0, 25.0) is t


class TestPlanRegionsSpeakerSwitch:
    """plan_regions: reframe_mode='speaker' → суб-деление шота на cut-переключения по репликам."""

    def _track(self, cx: float, hi: tuple[int, int]) -> object:
        from app.pipeline.stage3_reframe import SpeakerTrack

        sf = tuple(1.0 if hi[0] <= i < hi[1] else -1.0 for i in range(180))
        return SpeakerTrack(
            f0=0, f1=180, cx=tuple([cx] * 180), width=0.12, speak=0.0, speak_frames=sf
        )

    def test_two_speakers_sub_divided_into_cuts(self) -> None:
        from app.pipeline.speaker_switch import SpeakerTurn
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0.2, (0, 90)), self._track(0.65, (90, 180))]
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(1, 3.0, 6.0, 10)]
        regs = plan_regions(
            [(0, 180)], tracks, fps=30.0, crop_w_frac=0.316, turns=turns, reframe_mode="speaker"
        )
        assert [r.mode for r in regs] == ["fill", "fill"]
        assert abs(regs[0].t1 - 3.0) < 1e-6
        assert regs[0].points[0].cx < 0.4 and regs[1].points[0].cx > 0.5  # разные лица

    def test_backcompat_turns_none_identical_to_default(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0.2, (0, 90)), self._track(0.65, (90, 180))]
        r_default = plan_regions([(0, 180)], tracks, fps=30.0, crop_w_frac=0.316)
        r_none = plan_regions(
            [(0, 180)], tracks, fps=30.0, crop_w_frac=0.316, turns=None, reframe_mode="auto"
        )
        assert r_default == r_none  # новые параметры без speaker-режима → байт-в-байт

    def test_spurious_lowcoverage_lane_ignored(self) -> None:
        from app.pipeline.speaker_switch import SpeakerTurn
        from app.pipeline.stage3_reframe import SpeakerTrack, plan_regions

        # спурьез-трек: 5 кадров (2.7% покрытия) с высоким ASD — coverage-гейт должен его выкинуть,
        # binding остаётся на 2 реальных лицах (иначе привязка к мусору = кроп не на том).
        spur = SpeakerTrack(
            f0=40, f1=45, cx=(0.5,) * 5, width=0.12, speak=1.0, speak_frames=(1.0,) * 5
        )
        tracks = [self._track(0.2, (0, 90)), spur, self._track(0.65, (90, 180))]
        turns = [SpeakerTurn(0, 0.0, 3.0, 10), SpeakerTurn(1, 3.0, 6.0, 10)]
        regs = plan_regions(
            [(0, 180)], tracks, fps=30.0, crop_w_frac=0.316, turns=turns, reframe_mode="speaker"
        )
        assert [r.mode for r in regs] == ["fill", "fill"]
        assert regs[0].points[0].cx < 0.4 and regs[1].points[0].cx > 0.5

    def test_single_speaker_falls_through_to_default(self) -> None:
        from app.pipeline.speaker_switch import SpeakerTurn
        from app.pipeline.stage3_reframe import plan_regions

        # один привязанный спикер (второй лейн не говорит) → None → обычная логика (fit на 2 лица)
        tracks = [self._track(0.2, (0, 180)), self._track(0.65, (0, 0))]
        turns = [SpeakerTurn(0, 0.0, 6.0, 20)]
        regs_sp = plan_regions(
            [(0, 180)], tracks, fps=30.0, crop_w_frac=0.316, turns=turns, reframe_mode="speaker"
        )
        regs_def = plan_regions([(0, 180)], tracks, fps=30.0, crop_w_frac=0.316)
        assert regs_sp == regs_def


class TestComputeCropWindow:
    def test_1080p_center(self) -> None:
        c = compute_crop_window(1920, 1080, 0.5, t=0.0)
        assert c.h == 1080
        assert c.w == 608  # round(1080*9/16) = round(607.5)
        assert c.y == 0
        # центр: x = round(0.5*1920 - 304) = 656, в пределах [0, 1920-608]
        assert c.x == 656
        assert 0 <= c.x and c.x + c.w <= 1920

    def test_aspect_is_9_16_within_1px(self) -> None:
        c = compute_crop_window(1920, 1080, 0.5, t=0.0)
        assert abs(c.w * 16 - c.h * 9) <= 16  # ≈ 9:16 с точностью до пикселя

    def test_clamps_left_edge(self) -> None:
        c = compute_crop_window(1920, 1080, 0.0, t=0.0)
        assert c.x == 0

    def test_clamps_right_edge(self) -> None:
        c = compute_crop_window(1920, 1080, 1.0, t=0.0)
        assert c.x == 1920 - c.w
        assert c.x + c.w <= 1920

    def test_center_fraction_clamped(self) -> None:
        c = compute_crop_window(1920, 1080, 1.5, t=0.0)
        assert c.x + c.w <= 1920

    def test_source_too_narrow_raises(self) -> None:
        with pytest.raises(JobError):
            compute_crop_window(500, 1080, 0.5, t=0.0)

    def test_bad_dims_raise(self) -> None:
        with pytest.raises(JobError):
            compute_crop_window(0, 1080, 0.5, t=0.0)


class TestShotIsWide:
    def test_empty_not_wide(self) -> None:
        assert shot_is_wide([], crop_w_frac=0.32) is False

    def test_single_face_frames_not_wide(self) -> None:
        assert shot_is_wide([[0.5], [0.52], [0.48]], crop_w_frac=0.32) is False

    def test_two_spread_faces_wide(self) -> None:
        assert shot_is_wide([[0.3, 0.7], [0.3, 0.7]], crop_w_frac=0.32) is True

    def test_two_close_faces_not_wide(self) -> None:
        assert shot_is_wide([[0.45, 0.55], [0.45, 0.55]], crop_w_frac=0.32) is False

    def test_majority_wide_triggers(self) -> None:
        assert shot_is_wide([[0.3, 0.7], [0.5]], crop_w_frac=0.32) is True


class TestSmoothCenters:
    """Exponential smoothing: нет лица → держим последний; первый без лица → 0.5."""

    def test_smoothing_zero_freezes(self) -> None:
        # smoothing=0 → camera never moves (last = last + 0*(cx-last) = last всегда)
        result = smooth_centers([None, 0.3, 0.7], smoothing=0.0)
        assert result[0] == 0.5  # нет лица, дефолт 0.5
        assert result[1] == 0.5  # smoothing=0 → заморожено на 0.5
        assert result[2] == 0.5  # всё ещё 0.5

    def test_no_face_holds_last(self) -> None:
        result = smooth_centers([0.6, None, None], smoothing=1.0)
        assert result[0] == pytest.approx(0.5 + 1.0 * (0.6 - 0.5))  # = 0.6
        assert result[1] == result[0]  # нет лица → держим
        assert result[2] == result[0]  # нет лица → держим

    def test_first_without_face_defaults_to_half(self) -> None:
        result = smooth_centers([None, None, 0.8], smoothing=0.15)
        assert result[0] == 0.5  # нет лица сначала → 0.5
        assert result[1] == 0.5  # всё ещё нет лица
        assert result[2] == pytest.approx(0.5 + 0.15 * (0.8 - 0.5))  # плавно к 0.8

    def test_smoothing_toward_target(self) -> None:
        result = smooth_centers([0.8], smoothing=0.15)
        # last=0.5; 0.5 + 0.15*(0.8-0.5) = 0.5 + 0.045 = 0.545
        assert result[0] == pytest.approx(0.545)

    def test_smoothing_one_jumps(self) -> None:
        result = smooth_centers([0.3, 0.9], smoothing=1.0)
        assert result[0] == pytest.approx(0.3)
        assert result[1] == pytest.approx(0.9)

    def test_empty_returns_empty(self) -> None:
        assert smooth_centers([]) == []


class TestClassifyFrame:
    """fit/fill по геометрии лиц одного кадра."""

    def test_no_faces_is_fit(self) -> None:
        assert classify_frame([], crop_w_frac=0.32) == "fit"

    def test_single_face_is_fill(self) -> None:
        assert classify_frame([(0.5, 0.1)], crop_w_frac=0.32) == "fill"

    def test_two_spread_faces_is_fit(self) -> None:
        # размах 0.7-0.3=0.4 > 0.32 → широко
        assert classify_frame([(0.3, 0.1), (0.7, 0.1)], crop_w_frac=0.32) == "fit"

    def test_two_close_faces_is_fill(self) -> None:
        # размах 0.55-0.45=0.1 < 0.32 → кластер → fill
        assert classify_frame([(0.45, 0.1), (0.55, 0.1)], crop_w_frac=0.32) == "fill"


class TestMergeShortRegions:
    """Анти-флеш V2: регион < min_hold_sec поглощается предыдущим."""

    def test_empty(self) -> None:
        assert merge_short_regions([], min_hold_sec=1.5) == []

    def test_single_unchanged(self) -> None:
        reg = [TrackRegion(0.0, 5.0, "fill", ())]
        assert merge_short_regions(reg, min_hold_sec=1.5) == reg

    def test_short_region_absorbed(self) -> None:
        prev_pt = TrackPoint(t=0.0, mode="fill", cx=0.5)
        regions = [
            TrackRegion(0.0, 3.0, "fill", (prev_pt,)),
            TrackRegion(3.0, 3.4, "fit", ()),  # 0.4с < 1.5 → поглощается
            TrackRegion(3.4, 6.0, "fill", ()),
        ]
        result = merge_short_regions(regions, min_hold_sec=1.5)
        # fit-регион поглощён в предыдущий fill → [0,3.4 fill, 3.4,6 fill]
        assert len(result) == 2
        assert result[0].t0 == 0.0
        assert result[0].t1 == 3.4
        assert result[0].mode == "fill"
        assert result[1].t0 == 3.4

    def test_long_regions_kept(self) -> None:
        regions = [
            TrackRegion(0.0, 3.0, "fill", ()),
            TrackRegion(3.0, 6.0, "fit", ()),  # 3с ≥ 1.5 → остаётся
        ]
        assert merge_short_regions(regions, min_hold_sec=1.5) == regions

    def test_first_short_not_absorbed(self) -> None:
        # первый регион короткий — нет предыдущего → остаётся
        regions = [
            TrackRegion(0.0, 0.3, "fit", ()),
            TrackRegion(0.3, 5.0, "fill", ()),
        ]
        result = merge_short_regions(regions, min_hold_sec=1.5)
        assert result[0].t0 == 0.0  # первый не поглощён


class TestBuildShotsFrames:
    def test_no_cuts_one_shot(self) -> None:
        from app.pipeline.stage3_reframe import build_shots_frames

        assert build_shots_frames([], total_frames=150) == [(0, 150)]

    def test_cuts_split_into_frame_intervals(self) -> None:
        from app.pipeline.stage3_reframe import build_shots_frames

        # склейки на кадрах 50 и 100 → 3 шота в КАДРАХ
        assert build_shots_frames([50, 100], total_frames=150) == [(0, 50), (50, 100), (100, 150)]

    def test_dedup_and_sort(self) -> None:
        from app.pipeline.stage3_reframe import build_shots_frames

        assert build_shots_frames([100, 50, 50], total_frames=150) == [
            (0, 50),
            (50, 100),
            (100, 150),
        ]

    def test_cuts_at_bounds_ignored(self) -> None:
        from app.pipeline.stage3_reframe import build_shots_frames

        # склейка на 0 и на total — не порождают пустых шотов
        assert build_shots_frames([0, 150], total_frames=150) == [(0, 150)]

    def test_zero_total_empty(self) -> None:
        from app.pipeline.stage3_reframe import build_shots_frames

        assert build_shots_frames([10], total_frames=0) == []


class TestSamplesInShot:
    def test_filters_to_interval_half_open(self) -> None:
        from app.pipeline.stage3_reframe import samples_in_shot

        raw = [(0.0, [(0.5, 0.1)]), (0.2, [(0.4, 0.1)]), (0.4, []), (0.6, [(0.7, 0.1)])]
        # интервал [0.2, 0.6): берём t=0.2 и t=0.4, НЕ берём 0.0 и 0.6
        got = samples_in_shot(raw, 0.2, 0.6)
        assert [t for t, _ in got] == [0.2, 0.4]

    def test_empty_when_no_samples_in_range(self) -> None:
        from app.pipeline.stage3_reframe import samples_in_shot

        assert samples_in_shot([(0.0, []), (5.0, [])], 1.0, 2.0) == []


class TestDecideShotMode:
    def test_no_samples_is_fit(self) -> None:
        from app.pipeline.stage3_reframe import decide_shot_mode

        assert decide_shot_mode([], crop_w_frac=0.3) == "fit"

    def test_single_face_cluster_is_fill(self) -> None:
        from app.pipeline.stage3_reframe import decide_shot_mode

        # одно лицо в каждом кадре → fill
        samples = [(0.0, [(0.5, 0.1)]), (0.2, [(0.52, 0.1)]), (0.4, [(0.48, 0.1)])]
        assert decide_shot_mode(samples, crop_w_frac=0.3) == "fill"

    def test_two_spread_faces_majority_is_fit(self) -> None:
        from app.pipeline.stage3_reframe import decide_shot_mode

        # 2 разнесённых лица (размах 0.6 > crop_w_frac 0.3) в большинстве кадров → fit
        wide = [(0.1, 0.1), (0.7, 0.1)]
        samples = [(0.0, wide), (0.2, wide), (0.4, [(0.5, 0.1)])]
        assert decide_shot_mode(samples, crop_w_frac=0.3) == "fit"

    def test_mode_setting_overrides(self) -> None:
        from app.pipeline.stage3_reframe import decide_shot_mode

        wide = [(0.1, 0.1), (0.7, 0.1)]
        assert decide_shot_mode([(0.0, wide)], crop_w_frac=0.3, mode_setting="fill") == "fill"
        assert decide_shot_mode([(0.0, [(0.5, 0.1)])], crop_w_frac=0.3, mode_setting="fit") == "fit"


class TestBuildShotTrajectory:
    def test_returns_trackpoints_with_smoothed_cx(self) -> None:
        from app.pipeline.stage3_reframe import build_shot_trajectory

        samples = [(1.0, [(0.2, 0.1)]), (1.2, [(0.8, 0.1)])]
        pts = build_shot_trajectory(samples, smoothing=0.5)
        assert len(pts) == 2
        assert pts[0].t == 1.0 and pts[0].mode == "fill"
        # init=first face cx=0.2; первый сэмпл: 0.2 + 0.5*(0.2-0.2) = 0.2 (снэп)
        assert abs(pts[0].cx - 0.2) < 1e-9
        # второй: 0.2 + 0.5*(0.8-0.2) = 0.5
        assert abs(pts[1].cx - 0.5) < 1e-9

    def test_largest_face_chosen(self) -> None:
        from app.pipeline.stage3_reframe import build_shot_trajectory

        # два лица: крупнейшее (w=0.3) на cx=0.9 → к нему ведём
        pts = build_shot_trajectory([(0.0, [(0.1, 0.1), (0.9, 0.3)])], smoothing=1.0)
        assert abs(pts[0].cx - 0.9) < 1e-9

    def test_no_face_holds_last(self) -> None:
        from app.pipeline.stage3_reframe import build_shot_trajectory

        pts = build_shot_trajectory([(0.0, [(0.2, 0.1)]), (0.2, [])], smoothing=1.0)
        # второй сэмпл без лица → держим последний (0.2)
        assert abs(pts[1].cx - 0.2) < 1e-9

    def test_first_point_snaps_to_face_not_center(self) -> None:
        from app.pipeline.stage3_reframe import build_shot_trajectory

        # Face at cx=0.9 (far from center 0.5). With smoothing=0.15, old code:
        # first = 0.5 + 0.15*(0.9 - 0.5) = 0.56 (drift from center).
        # New code: init=0.9, first = 0.9 + 0.15*(0.9 - 0.9) = 0.9 (snap to face).
        pts = build_shot_trajectory([(0.0, [(0.9, 0.1)])], smoothing=0.15)
        assert abs(pts[0].cx - 0.9) < 1e-6  # must snap, not drift


class TestBuildRegionsFromShots:
    def test_one_mode_per_shot_cut_aligned(self) -> None:
        from app.pipeline.stage3_reframe import build_regions_from_shots

        # 3 плана по реальным склейкам; средний -- широкий (2 разнесённых лица)
        shots = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
        single = [(0.5, 0.1)]
        wide = [(0.1, 0.1), (0.8, 0.1)]
        raw = [
            (0.0, single),
            (1.0, single),  # план 1 -> fill
            (2.0, wide),
            (3.0, wide),  # план 2 -> fit
            (4.0, single),
            (5.0, single),  # план 3 -> fill
        ]
        regions = build_regions_from_shots(
            shots, raw, crop_w_frac=0.3, smoothing=0.15, min_hold_sec=0.0
        )
        assert [(r.t0, r.t1, r.mode) for r in regions] == [
            (0.0, 2.0, "fill"),
            (2.0, 4.0, "fit"),
            (4.0, 6.0, "fill"),
        ]
        # границы режима = границы планов (= реальные склейки), НЕ сетка сэмплов
        assert regions[0].points and regions[2].points  # fill-планы имеют траекторию
        assert regions[1].points == ()  # fit-план без траектории

    def test_short_shot_absorbed_by_min_hold(self) -> None:
        from app.pipeline.stage3_reframe import build_regions_from_shots

        # короткий средний план (0.3с < min_hold 1.5) поглощается предыдущим -> нет дрожи
        shots = [(0.0, 2.0), (2.0, 2.3), (2.3, 4.0)]
        single = [(0.5, 0.1)]
        wide = [(0.1, 0.1), (0.8, 0.1)]
        raw = [(0.0, single), (2.0, wide), (2.3, single)]
        regions = build_regions_from_shots(
            shots, raw, crop_w_frac=0.3, smoothing=0.15, min_hold_sec=1.5
        )
        assert all(r.mode == "fill" for r in regions)  # короткий fit съеден

    def test_fill_without_faces_has_fallback_point(self) -> None:
        from app.pipeline.stage3_reframe import build_regions_from_shots

        # mode_setting=fill форсит fill даже без лиц -> должна быть точка-фолбэк (cx=0.5)
        regions = build_regions_from_shots(
            [(0.0, 2.0)],
            [(0.0, [])],
            crop_w_frac=0.3,
            smoothing=0.15,
            min_hold_sec=0.0,
            mode_setting="fill",
        )
        assert regions[0].mode == "fill"
        assert regions[0].points and abs(regions[0].points[0].cx - 0.5) < 1e-9


class TestPlanRegions:
    """plan_regions: shots(кадры) + SpeakerTrack → TrackRegion, решение на шот."""

    def _track(self, f0: int, f1: int, cx: float, width: float, speak: float):  # type: ignore[no-untyped-def]
        from app.pipeline.stage3_reframe import SpeakerTrack

        n = f1 - f0
        return SpeakerTrack(f0=f0, f1=f1, cx=tuple([cx] * n), width=width, speak=speak)

    def test_single_speaker_fill_on_track(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # один шот [0,30 кадров), один говорящий на cx=0.7
        tracks = [self._track(0, 30, 0.7, 0.12, 0.9)]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, speak_threshold=0.0
        )
        assert len(regions) == 1
        assert regions[0].mode == "fill"
        assert regions[0].points  # есть траектория
        assert abs(regions[0].points[0].cx - 0.7) < 1e-9

    def test_two_spread_no_clear_speaker_is_fit(self) -> None:
        # 2 разнесённых лица, НИКТО явно не говорит (speak < wide_speak_min) → fit (split off).
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 30, 0.2, 0.1, 0.1),
            self._track(0, 30, 0.8, 0.1, 0.1),
        ]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32)
        assert regions[0].mode == "fit"
        assert regions[0].points == ()

    def test_wide_two_people_goes_wide_even_if_speaking(self) -> None:
        # Фикс фаундера: 2 разнесённых лица → ШИРОКО (fit/split), ДАЖЕ если один явно говорит.
        # ИИ не должен tight-кропить 2-shot (теряет второго, кадрирует ноги/торс).
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 30, 0.2, 0.1, -0.5),  # молчит
            self._track(0, 30, 0.8, 0.1, 0.8),  # говорит явно — но всё равно НЕ fill
        ]
        # split выключен (дефолт) → fit (весь кадр + блюр = «широкий»)
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, speak_threshold=0.0
        )
        assert regions[0].mode == "fit"
        assert regions[0].points == ()
        # split включён → split (оба full-bleed), не кроп на говорящем
        split = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, split_enabled=True
        )
        assert split[0].mode == "split"

    def test_typical_interview_2shot_goes_wide(self) -> None:
        # Реальный кейс (репро на Tom Holland × Jay Shetty): два человека в креслах, лица на
        # cx~0.36 и cx~0.64 → размах 0.28. Старый порог = crop_w_frac (~0.317) → НЕ wide → tight-
        # кроп на «говорящем» (терял второго). Новый порог = crop_w_frac*0.70 (~0.222) → WIDE.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 50, 0.36, 0.12, 0.6),  # говорит
            self._track(0, 50, 0.64, 0.12, 0.2),
        ]
        regions = plan_regions([(0, 50)], tracks, fps=25.0, crop_w_frac=0.3167, speak_threshold=0.0)
        assert regions[0].mode == "fit"  # WIDE, не tight-кроп на одном

    def test_ghost_short_track_does_not_fake_wide(self) -> None:
        # Реальный кейс (репро на Tom Holland): одиночный план, но трекер плодит КОРОТКИЙ
        # спурьез-трек (покрытие ~10% шота: отражение/фон/разрыв IOU) → раньше подделывал 2-shot
        # → ЛОЖНЫЙ wide на ОДНОМ человеке. Coverage-гейт его отбрасывает → fill (tight на субъекте).
        from app.pipeline.stage3_reframe import plan_regions

        real = self._track(0, 50, 0.5, 0.12, 0.8)
        ghost = self._track(0, 5, 0.15, 0.12, 0.0)  # покрытие 5/50 = 10% < 25% → спурьез
        regions = plan_regions(
            [(0, 50)], [real, ghost], fps=25.0, crop_w_frac=0.3167, speak_threshold=0.0
        )
        assert regions[0].mode == "fill"  # призрак отброшен → один субъект → tight

    def test_wide_clear_speaker_below_min_stays_fit(self) -> None:
        # говорит, но слабо (< wide_speak_min) → не «явный» → fit (широкий план).
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 30, 0.2, 0.1, 0.1),
            self._track(0, 30, 0.8, 0.1, 0.2),
        ]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, wide_speak_min=0.3)
        assert regions[0].mode == "fit"

    def test_picks_louder_speaker_not_largest(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # ДВА лица КЛАСТЕРОМ (размах 0.15 < crop_w 0.32 → не широко). Крупнее (width 0.3) на
        # cx=0.45 молчит; говорит мелкое (width 0.1) на cx=0.60 → кадр на говорящего.
        tracks = [
            self._track(0, 30, 0.45, 0.3, 0.1),
            self._track(0, 30, 0.60, 0.1, 0.95),
        ]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, speak_threshold=0.3
        )
        assert regions[0].mode == "fill"
        assert abs(regions[0].points[0].cx - 0.60) < 1e-9  # на говорящего, не на крупнейшего

    def test_asd_silent_falls_back_to_largest_face(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # Кластер (не широко); никто не превышает порог → берём крупнейшее лицо (width 0.3, cx=0.45)
        tracks = [
            self._track(0, 30, 0.45, 0.3, 0.05),
            self._track(0, 30, 0.60, 0.1, 0.10),
        ]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, speak_threshold=0.5
        )
        assert regions[0].mode == "fill"
        assert abs(regions[0].points[0].cx - 0.45) < 1e-9  # фолбэк на largest-face

    def test_no_tracks_is_fit(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        regions = plan_regions([(0, 30)], [], fps=30.0, crop_w_frac=0.32)
        assert regions[0].mode == "fit"

    def test_subject_cx_tiebreaks_cluster_over_speak(self) -> None:
        # B2: два лица КЛАСТЕРОМ (размах 0.15 < spread_min → не wide → fill-путь). Громче левое
        # (cx=0.45, speak 0.9), но vision-subject возле правого (0.60) → кроп на правое лицо.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.45, 0.1, 0.9), self._track(0, 30, 0.60, 0.1, 0.1)]
        r0 = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, speak_threshold=0.0
        )
        assert abs(r0[0].points[0].cx - 0.45) < 1e-9  # без subject → громче
        r1 = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0,
            speak_threshold=0.0, subject_cx=0.60,
        )  # fmt: skip
        assert abs(r1[0].points[0].cx - 0.60) < 1e-9  # vision-subject → правое лицо

    def test_low_coverage_face_is_fit_not_legs(self) -> None:
        # Хвост дорожки, заползший на 3/100 кадров за склейку в face-sparse establishing-план
        # (покрытие 3%), НЕ субъект → fit (wide), а НЕ tight-кроп в «ноги»/торс. Репро на реальном
        # CARDI B B-roll: говорящий бьётся 3 кадра в 7.9с реклайн-план → планировщик кропил ноги.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 3, 0.63, 0.12, 0.9)]
        regions = plan_regions([(0, 100)], tracks, fps=30.0, crop_w_frac=0.32, speak_threshold=0.0)
        assert regions[0].mode == "fit"
        assert regions[0].points == ()

    def test_partial_coverage_above_floor_stays_fill(self) -> None:
        # Лицо появляется на 40% шота (вошёл в кадр позже) — реальный субъект → fill, не wide.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 40, 0.5, 0.12, 0.9)]
        regions = plan_regions([(0, 100)], tracks, fps=30.0, crop_w_frac=0.32, speak_threshold=0.0)
        assert regions[0].mode == "fill"

    def test_tiny_silent_face_is_fit(self) -> None:
        # «ambiguous → horizontal»: одно МЕЛКОЕ лицо (width < _MIN_FACE_FRAC=0.08) и молчит
        # (speak < threshold) → нет уверенного субъекта → fit (не кропим в никуда).
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.5, 0.05, 0.0)]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, speak_threshold=0.3)
        assert regions[0].mode == "fit"
        assert regions[0].points == ()

    def test_large_silent_face_is_fill(self) -> None:
        # Лицо крупное (width ≥ _MIN_FACE_FRAC=0.08), пусть и молчит → уверенный субъект → fill.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.6, 0.12, 0.0)]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, speak_threshold=0.3
        )
        assert regions[0].mode == "fill"
        assert abs(regions[0].points[0].cx - 0.6) < 1e-9

    def test_tiny_clear_speaker_is_fill(self) -> None:
        # Лицо мелкое (width < 0.08), НО явно говорит (speak ≥ threshold) → уверенный → fill.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.4, 0.05, 0.9)]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, speak_threshold=0.3
        )
        assert regions[0].mode == "fill"
        assert abs(regions[0].points[0].cx - 0.4) < 1e-9

    def test_fill_snaps_to_new_speaker_at_cut(self) -> None:
        """Поперёк РЕАЛЬНОЙ склейки новый fill-шот ОТКРЫВАЕТСЯ на СВОЁМ говорящем, а НЕ
        «доезжает» от центра прошлого шота.

        Шот1 говорящий @0.2, шот2 @0.8. Прежняя «непрерывность» (ebfc3dc) инитила EMA шота2
        от конца шота1 (≈0.2) → кроп ОТКРЫВАЛСЯ между двумя людьми и медленно полз к 0.8 =
        «пол-лица» в начале КАЖДОЙ склейки спикера (жалоба фаундера, репро на Tom Holland:
        gap traj_start↔face до 0.28). Жёсткий cut и так перерисовывает весь кадр → снап кропа
        на склейке НЕВИДИМ. Сглаживание ВНУТРИ шота (EMA) не тронуто. Кадровая сетка цела.
        """
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 25, 0.2, 0.12, 0.9),
            self._track(25, 50, 0.8, 0.12, 0.9),
        ]
        regions = plan_regions(
            [(0, 25), (25, 50)], tracks, fps=25.0, crop_w_frac=0.32, smoothing=0.15
        )
        assert len(regions) == 2
        assert all(r.mode == "fill" for r in regions)
        # шот1 откроется на спикере A (0.2), шот2 — на B (0.8): лицо целиком с кадра 0
        assert abs(regions[0].points[0].cx - 0.2) < 1e-9
        assert abs(regions[1].points[0].cx - 0.8) < 1e-9

    def test_small_silent_face_goes_wide(self) -> None:
        """Одно МЕЛКОЕ молчащее лицо (speak сильно <0 И width < _SILENCE_MIN_FILL_FACE=0.12) =
        настоящий широкий/establishing/legs-план → fit (wide), НЕ strip на теле/ногах. Мелкое лицо
        = человек далёк/мал в кадре = реально широкий план (речь тут не единственный сигнал)."""
        from app.pipeline.stage3_reframe import plan_regions

        # мелкое лицо (0.09 < 0.12) + молчит (−1.7) → широкий план
        tracks = [self._track(0, 50, 0.4, 0.09, -1.7)]
        regions = plan_regions([(0, 50)], tracks, fps=25.0, crop_w_frac=0.3167, speak_threshold=0.0)
        assert regions[0].mode == "fit"
        assert regions[0].points == ()

    def test_large_silent_reaction_fills(self) -> None:
        """Одно КРУПНОЕ лицо (width ≥ _SILENCE_MIN_FILL_FACE=0.12), пусть и уверенно МОЛЧИТ
        (speak −1.7) → fill (наводимся на лицо). Молчащая реакция слушателя крупным планом
        (интервьюер Cardi B, лицо ~0.15) — в кадре ОДИН человек, кропим его, не wide. Фаундер-QA."""
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 50, 0.6, 0.15, -1.7)]
        regions = plan_regions(
            [(0, 50)], tracks, fps=25.0, crop_w_frac=0.3167, smoothing=1.0, speak_threshold=0.0
        )
        assert regions[0].mode == "fill"
        assert abs(regions[0].points[0].cx - 0.6) < 1e-9

    def test_single_face_no_asd_score_is_fill(self) -> None:
        """speak == _SILENT(-9) = ASD ПРОПУЩЕН (одна дорожка в сегменте, perf) = «скор не считался»,
        НЕ «молчит». Один однозначный говорящий → FILL (tight), а НЕ wide (иначе talking-head клип с
        одним планом ошибочно уходил в wide — репро Tom Holland clip_1: speak=-9 → был wide)."""
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 50, 0.4, 0.169, -9.0)]
        regions = plan_regions([(0, 50)], tracks, fps=25.0, crop_w_frac=0.3167, speak_threshold=0.0)
        assert regions[0].mode == "fill"
        assert abs(regions[0].points[0].cx - 0.4) < 1e-9

    def test_fill_glides_when_next_speaker_is_near(self) -> None:
        """Hybrid: два подряд fill-шота, лицо РЯДОМ (Δcx мал) → 2-й ДОЕЗЖАЕТ от центра прошлого
        (continuity, плавно), НЕ снапает. Снап только на ДАЛЁКОМ прыжке (см.
        test_fill_snaps_to_new_speaker_at_cut). Так возвращаем плавное наведение без пол-лица."""
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 25, 0.50, 0.16, 0.9),
            self._track(25, 50, 0.56, 0.16, 0.9),  # рядом (Δ=0.06)
        ]
        regions = plan_regions(
            [(0, 25), (25, 50)],
            tracks,
            fps=25.0,
            crop_w_frac=0.3167,
            smoothing=0.15,
            lock_per_shot=False,
        )
        assert all(r.mode == "fill" for r in regions)
        # 2-й стартует БЛИЗКО к концу 1-го (continuity) — legacy follower под lock_per_shot=False
        assert abs(regions[1].points[0].cx - regions[0].points[-1].cx) <= 0.05

    def test_split_two_spread_stable_tracks(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # 2 устойчивых разнесённых трека, НИКТО явно не говорит (speak<0.3) + split_enabled →
        # split (вместо fit); обе траектории; порядок стабилен по cx (левый → points).
        tracks = [
            self._track(0, 30, 0.8, 0.1, 0.1),
            self._track(0, 30, 0.2, 0.1, 0.1),
        ]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, split_enabled=True
        )
        assert regions[0].mode == "split"
        assert regions[0].points and regions[0].points_b
        assert abs(regions[0].points[0].cx - 0.2) < 1e-9  # левый сверху
        assert abs(regions[0].points_b[0].cx - 0.8) < 1e-9

    def test_split_disabled_falls_back_fit(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 30, 0.2, 0.1, 0.1),
            self._track(0, 30, 0.8, 0.1, 0.1),
        ]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, split_enabled=False)
        assert regions[0].mode == "fit"

    def test_three_spread_faces_still_fit(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [
            self._track(0, 30, 0.15, 0.1, 0.1),
            self._track(0, 30, 0.5, 0.1, 0.1),
            self._track(0, 30, 0.85, 0.1, 0.1),
        ]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, split_enabled=True)
        assert regions[0].mode == "fit"

    def test_split_requires_stable_coverage(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # Второй трек живёт лишь 6 кадров из 30 (20% < coverage_min 25%) → coverage-гейт
        # _is_wide_shot его ОТБРАСЫВАЕТ → один реальный субъект (cx0.2) → fill (tight на нём),
        # а НЕ ложный wide на спурьез-треке. (Раньше считался любой трек → fit.)
        tracks = [
            self._track(0, 30, 0.2, 0.1, 0.1),
            self._track(0, 6, 0.8, 0.1, 0.1),
        ]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, split_enabled=True)
        assert regions[0].mode == "fill"

    def test_split_cluster_not_split(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # 2 трека КЛАСТЕРОМ (размах < crop_w_frac) → это fill-кейс, не split
        tracks = [
            self._track(0, 30, 0.45, 0.3, 0.9),
            self._track(0, 30, 0.55, 0.1, 0.1),
        ]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, split_enabled=True
        )
        assert regions[0].mode == "fill"

    def test_track_region_points_b_default_empty(self) -> None:
        assert TrackRegion(t0=0.0, t1=1.0, mode="fill", points=()).points_b == ()

    def test_build_regions_from_shots_split_two_spread_faces(self) -> None:
        from app.pipeline.stage3_reframe import build_regions_from_shots

        # editor-путь (sample-based): стабильно 2 разнесённых лица → split (агент сам)
        raw = [(i / 5.0, [(0.2, 0.1), (0.8, 0.1)]) for i in range(10)]
        regions = build_regions_from_shots([(0.0, 2.0)], raw, 0.32, 0.15, 1.5, split_enabled=True)
        assert regions[0].mode == "split"
        assert regions[0].points[0].cx is not None and regions[0].points[0].cx < 0.5
        assert regions[0].points_b[0].cx is not None and regions[0].points_b[0].cx > 0.5

    def test_build_regions_from_shots_split_disabled_is_fit(self) -> None:
        from app.pipeline.stage3_reframe import build_regions_from_shots

        raw = [(i / 5.0, [(0.2, 0.1), (0.8, 0.1)]) for i in range(10)]
        regions = build_regions_from_shots([(0.0, 2.0)], raw, 0.32, 0.15, 1.5, split_enabled=False)
        assert regions[0].mode == "fit"

    def test_build_regions_from_shots_three_faces_stays_fit(self) -> None:
        from app.pipeline.stage3_reframe import build_regions_from_shots

        raw = [(i / 5.0, [(0.15, 0.1), (0.5, 0.1), (0.85, 0.1)]) for i in range(10)]
        regions = build_regions_from_shots([(0.0, 2.0)], raw, 0.32, 0.15, 1.5, split_enabled=True)
        assert regions[0].mode == "fit"

    def test_build_regions_from_shots_unstable_pair_stays_fit(self) -> None:
        from app.pipeline.stage3_reframe import build_regions_from_shots

        # второй человек виден лишь на 3 из 10 сэмплов (<60%) → НЕ split
        # (одно лицо доминирует → корректный режим fill на спикере)
        raw = [(i / 5.0, [(0.2, 0.1), (0.8, 0.1)] if i < 3 else [(0.2, 0.1)]) for i in range(10)]
        regions = build_regions_from_shots([(0.0, 2.0)], raw, 0.32, 0.15, 1.5, split_enabled=True)
        assert regions[0].mode != "split"

    def test_merge_short_regions_preserves_points_b(self) -> None:
        from app.pipeline.stage3_reframe import merge_short_regions

        pa = (TrackPoint(t=0.0, mode="split", cx=0.2),)
        pb = (TrackPoint(t=0.0, mode="split", cx=0.8),)
        long_split = TrackRegion(t0=0.0, t1=5.0, mode="split", points=pa, points_b=pb)
        short_fit = TrackRegion(t0=5.0, t1=5.5, mode="fit", points=())
        out = merge_short_regions([long_split, short_fit], min_hold_sec=1.5)
        assert len(out) == 1
        assert out[0].mode == "split" and out[0].points_b == pb
        assert out[0].t1 == 5.5

    def test_speaker_change_between_shots(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # шот1 [0,30): говорит A(cx0.3); шот2 [30,60): говорит B(cx0.7)
        tracks = [
            self._track(0, 30, 0.3, 0.1, 0.9),
            self._track(30, 60, 0.7, 0.1, 0.9),
        ]
        regions = plan_regions(
            [(0, 30), (30, 60)],
            tracks,
            fps=30.0,
            crop_w_frac=0.32,
            smoothing=1.0,
            speak_threshold=0.0,
        )
        assert len(regions) == 2
        assert abs(regions[0].points[0].cx - 0.3) < 1e-9
        assert abs(regions[1].points[0].cx - 0.7) < 1e-9
        # границы регионов = границы шотов в СЕКУНДАХ (кадр/fps)
        assert regions[0].t0 == 0.0 and regions[0].t1 == 1.0
        assert regions[1].t0 == 1.0 and regions[1].t1 == 2.0

    def test_mode_setting_fit_overrides(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.5, 0.1, 0.9)]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, mode_setting="fit")
        assert regions[0].mode == "fit"

    def test_mode_setting_fill_no_track_fallback_center(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        regions = plan_regions([(0, 30)], [], fps=30.0, crop_w_frac=0.32, mode_setting="fill")
        assert regions[0].mode == "fill"
        assert regions[0].points and abs(regions[0].points[0].cx - 0.5) < 1e-9


class TestPickTargetVisionSubject:
    """_pick_target: vision subject-x tie-breaker (B2). Pure, off-by-default via caller."""

    def _track(self, cx: float, speak: float, width: float = 0.1):  # type: ignore[no-untyped-def]
        from app.pipeline.stage3_reframe import SpeakerTrack

        return SpeakerTrack(f0=0, f1=10, cx=tuple([cx] * 10), width=width, speak=speak)

    def test_none_subject_uses_speak_heuristic(self) -> None:
        from app.pipeline.stage3_reframe import _pick_target

        loud, quiet = self._track(0.2, 0.9), self._track(0.8, 0.1)
        assert _pick_target([loud, quiet], 0.0) is loud  # backward-compatible: max speak

    def test_subject_picks_nearest_track_over_louder(self) -> None:
        from app.pipeline.stage3_reframe import _pick_target

        loud_far = self._track(0.2, 0.9)  # loudest but far from subject
        quiet_near = self._track(0.8, 0.1)  # quiet but near subject 0.82
        assert _pick_target([loud_far, quiet_near], 0.0, subject_cx=0.82) is quiet_near

    def test_subject_far_from_all_falls_through_to_speak(self) -> None:
        from app.pipeline.stage3_reframe import _pick_target

        # subject at 0.5, tracks at 0.1/0.9 (both ~0.4 away > threshold) → no confident match
        loud, quiet = self._track(0.1, 0.9), self._track(0.9, 0.1)
        assert _pick_target([loud, quiet], 0.0, subject_cx=0.5) is loud

    def test_empty_active_is_none(self) -> None:
        from app.pipeline.stage3_reframe import _pick_target

        assert _pick_target([], 0.0, subject_cx=0.5) is None


class TestFollowerSmoothing:
    """_track_trajectory (follower): центрированное сглаживание + верная трассировка кривой.
    Founder QA clip_04 — статичное лицо НЕ должно дрейфовать (баг разрежённых кейфреймов +
    линейной интерполяции); реальное движение — плавно."""

    def _track(self, cx_list: list[float], fps: float = 30.0) -> tuple:
        from app.pipeline.stage3_reframe import SpeakerTrack, _track_trajectory

        tr = SpeakerTrack(f0=0, f1=len(cx_list), cx=tuple(cx_list), width=0.15, speak=1.0)
        return _track_trajectory(tr, 0, len(cx_list), fps=fps, smoothing=0.1)

    def test_smooth_cx_centered_kills_noise(self) -> None:
        from app.pipeline.stage3_reframe import _smooth_cx_centered

        vals = [0.50, 0.56, 0.44, 0.53, 0.47, 0.51, 0.49, 0.50]  # покачивание вокруг 0.5
        out = _smooth_cx_centered(vals, win=5)
        assert len(out) == len(vals)
        assert all(abs(v - 0.5) < 0.03 for v in out)  # шум сглажен к среднему

    def test_static_face_does_not_drift(self) -> None:
        # лицо строго статично на 0.45 три секунды → кроп держит 0.45 всё время (ноль дрейфа).
        pts = self._track([0.45] * 90)
        assert all(abs(p.cx - 0.45) < 0.01 for p in pts)

    def test_static_then_move_holds_then_eases(self) -> None:
        # статика 0.45 (2с), затем рамп к 0.305 (1с) → рано ДЕРЖИТ ~0.45 (лицо в зоне), после выхода
        # за зону плавно доводит В СТОРОНУ лица (dead-zone → останавливается, не доводя до центра).
        cx = [0.45] * 60 + [0.45 - 0.005 * i for i in range(30)]
        pts = self._track(cx)
        early = [p.cx for p in pts if p.t < 1.5]
        assert early and all(abs(c - 0.45) < 0.03 for c in early)  # нет раннего дрейфа
        assert pts[-1].cx < 0.42  # в итоге сместился к лицу (но dead-zone → ~0.06 не доходя центра)
        assert pts[-1].cx > 0.30  # и не перелетел лицо

    def test_small_pan_no_step_facets(self) -> None:
        # Founder QA clip_07: медленный пан ЧУТЬ за dead-zone двигался «маленькими прыжками».
        # Причина: кейфреймы раз в 0.5с (_FOLLOW_SAMPLE_SEC) при min_delta 0.012 → линейная
        # интерполяция рендера давала СКАЧКИ СКОРОСТИ каждые 0.5с. Реконструируем per-frame как
        # рендер (линейный interp) и проверяем, что рывок (|Δскорости|) КРОШЕЧНЫЙ.
        fps = 30.0
        cx = [0.45 + 0.15 * min(1.0, i / 55) for i in range(75)] + [0.60] * 15
        pts = self._track(cx)

        def interp(t: float) -> float:
            if t <= pts[0].t:
                return pts[0].cx or 0.5
            for i in range(len(pts) - 1):
                if pts[i].t <= t <= pts[i + 1].t:
                    span = pts[i + 1].t - pts[i].t
                    a = (t - pts[i].t) / span if span > 0 else 0.0
                    return (pts[i].cx or 0.5) + a * ((pts[i + 1].cx or 0.5) - (pts[i].cx or 0.5))
            return pts[-1].cx or 0.5

        recon = [interp(f / fps) for f in range(len(cx))]
        vel = [recon[i] - recon[i - 1] for i in range(1, len(recon))]
        jerk = [abs(vel[i] - vel[i - 1]) for i in range(1, len(vel))]
        assert max(jerk) < 0.0015  # нет видимых скачков скорости на медленном пане
        assert (pts[-1].cx or 0) > (pts[0].cx or 0) + 0.05  # и пан реально произошёл


class TestEaseDeadZoneCamera:
    """_ease_dead_zone_camera: 2nd-order (velocity-damped) dead-zone follower (Layer B).

    Slow-in/slow-out: the camera accelerates smoothly INTO a pan (no first-frame lurch of the
    old exponential ease) and holds on a still face. Grid-safe (cx values only)."""

    def test_still_face_holds(self) -> None:
        from app.pipeline.stage3_reframe import _ease_dead_zone_camera

        cams = _ease_dead_zone_camera([0.45] * 60, None)
        assert all(abs(c - 0.45) < 1e-9 for c in cams)

    def test_empty_is_empty(self) -> None:
        from app.pipeline.stage3_reframe import _ease_dead_zone_camera

        assert _ease_dead_zone_camera([], None) == []

    def test_velocity_ramps_no_first_frame_lurch(self) -> None:
        # step target well outside the dead-zone: 2nd-order ramps velocity, so the FIRST-frame
        # move is far smaller than the exponential _FOLLOW_EASE*offset would give, and the
        # per-frame velocity keeps GROWING for several frames (accelerating into the pan).
        from app.pipeline.stage3_reframe import (
            _FOLLOW_ACCEL,
            _FOLLOW_EASE,
            _ease_dead_zone_camera,
        )

        sm = [0.5] + [0.8] * 40  # instant sustained offset of 0.30
        cams = _ease_dead_zone_camera(sm, None)
        v1 = cams[1] - cams[0]
        exp_first = _FOLLOW_EASE * (0.8 - 0.5)  # what the old exponential moved on frame 1
        assert 0.0 < v1 < exp_first * _FOLLOW_ACCEL * 2  # gradual, ~accel-scaled, not a lurch
        # velocity accelerates (slow-in): early per-frame deltas strictly increase
        deltas = [cams[i + 1] - cams[i] for i in range(1, 6)]
        assert all(deltas[k + 1] > deltas[k] for k in range(len(deltas) - 1))

    def test_reaches_toward_move_and_no_overshoot(self) -> None:
        from app.pipeline.stage3_reframe import _FOLLOW_DEADZONE, _ease_dead_zone_camera

        sm = [0.5] + [0.8] * 200
        cams = _ease_dead_zone_camera(sm, None)
        # eventually catches up to within the dead-zone of the target, never overshoots past it
        assert 0.8 - _FOLLOW_DEADZONE - 0.02 < cams[-1] <= 0.8 + 1e-9

    def test_init_cx_seeds_start(self) -> None:
        from app.pipeline.stage3_reframe import _ease_dead_zone_camera

        cams = _ease_dead_zone_camera([0.45] * 30, init_cx=0.60)
        assert cams[0] == 0.60


class TestVisionShotForSceneShot:
    """vision_shot_for_scene_shot: pure scene-shot↔vision-shot mapping (Layer A). Boundaries and
    the fps grid are NOT read for cuts — only midpoint→mode lookup."""

    def _vs(self, start: float, mode: str, cx: float | None = None):  # type: ignore[no-untyped-def]
        from app.pipeline.stage2b_vision_review import VisionShot

        return VisionShot(start_sec=start, mode=mode, subject_cx=cx)

    def test_empty_is_none(self) -> None:
        from app.pipeline.stage3_reframe import vision_shot_for_scene_shot

        assert vision_shot_for_scene_shot([], 0, 30, 30.0) is None

    def test_midpoint_selects_covering_shot(self) -> None:
        from app.pipeline.stage3_reframe import vision_shot_for_scene_shot

        vshots = [self._vs(0.0, "fill", 0.3), self._vs(2.0, "fit"), self._vs(5.0, "fill", 0.6)]
        # scene shot [90,120)@30fps → midpoint 3.5s → falls in [2.0,5.0) → the fit shot
        got = vision_shot_for_scene_shot(vshots, 90, 120, 30.0)
        assert got is not None and got.mode == "fit"

    def test_last_shot_covers_to_infinity(self) -> None:
        from app.pipeline.stage3_reframe import vision_shot_for_scene_shot

        vshots = [self._vs(0.0, "fill", 0.3), self._vs(5.0, "fill", 0.6)]
        got = vision_shot_for_scene_shot(vshots, 300, 360, 30.0)  # midpoint 11s → last shot
        assert got is not None and got.subject_cx == 0.6

    def test_midpoint_before_first_uses_nearest_first(self) -> None:
        from app.pipeline.stage3_reframe import vision_shot_for_scene_shot

        vshots = [self._vs(1.0, "fit"), self._vs(4.0, "fill", 0.5)]
        got = vision_shot_for_scene_shot(vshots, 0, 15, 30.0)  # midpoint 0.25s < 1.0 → first
        assert got is not None and got.mode == "fit"


class TestPlanRegionsVisionLayout:
    """plan_regions vision_shots override (Layer A): vision decides per-shot fill/fit + subject,
    overriding the silence/coverage heuristic gates. Boundaries/grid untouched. Default None →
    byte-identical (covered by every other TestPlanRegions* case running with vision_shots=None)."""

    def _track(self, f0: int, f1: int, cx: float, width: float, speak: float):  # type: ignore[no-untyped-def]
        from app.pipeline.stage3_reframe import SpeakerTrack

        n = f1 - f0
        return SpeakerTrack(f0=f0, f1=f1, cx=tuple([cx] * n), width=width, speak=speak)

    def _vs(self, start: float, mode: str, cx: float | None = None):  # type: ignore[no-untyped-def]
        from app.pipeline.stage2b_vision_review import VisionShot

        return VisionShot(start_sec=start, mode=mode, subject_cx=cx)

    def test_vision_fill_forces_fill_over_silence_gate(self) -> None:
        # small face (0.09 < _SILENCE_MIN_FILL_FACE) + confidently SILENT (−1.5): heuristic fits
        # (small+silent = wide); vision says fill → FORCE fill (bypasses the silence gate).
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 50, 0.5, 0.09, -1.5)]
        heuristic = plan_regions([(0, 50)], tracks, fps=25.0, crop_w_frac=0.3167)
        assert heuristic[0].mode == "fit"  # confirm the heuristic would widen it
        vis = plan_regions(
            [(0, 50)], tracks, fps=25.0, crop_w_frac=0.3167,
            vision_shots=[self._vs(0.0, "fill", 0.5)],
        )  # fmt: skip
        assert vis[0].mode == "fill"

    def test_vision_fill_forces_fill_over_low_coverage_gate(self) -> None:
        # face covers only ~10% of the shot (< _FILL_MIN_COVERAGE) → heuristic → fit; vision fill
        # bypasses the coverage gate and crops on the (only) active track.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 10, 0.4, 0.16, 0.9)]  # 10 of 100 frames
        heuristic = plan_regions([(0, 100)], tracks, fps=30.0, crop_w_frac=0.32)
        assert heuristic[0].mode == "fit"
        vis = plan_regions(
            [(0, 100)], tracks, fps=30.0, crop_w_frac=0.32,
            vision_shots=[self._vs(0.0, "fill", 0.4)],
        )  # fmt: skip
        assert vis[0].mode == "fill"

    def test_vision_fit_forces_fit_over_fill(self) -> None:
        # single clear speaker → heuristic fill; vision says fit (e.g. a wide establishing beat)
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.5, 0.16, 0.9)]
        heuristic = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32)
        assert heuristic[0].mode == "fill"
        vis = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, vision_shots=[self._vs(0.0, "fit")]
        )
        assert vis[0].mode == "fit"

    def test_vision_subject_cx_picks_track(self) -> None:
        # two clustered tracks; loud one at 0.2, quiet at 0.8; vision subject_cx 0.82 → pick quiet
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.2, 0.16, 0.9), self._track(0, 30, 0.8, 0.16, 0.1)]
        vis = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0,
            vision_shots=[self._vs(0.0, "fill", 0.82)],
        )  # fmt: skip
        assert vis[0].mode == "fill"
        assert abs(vis[0].points[0].cx - 0.8) < 1e-9  # cropped on the vision-chosen subject

    def test_no_vision_shot_falls_through_to_heuristics(self) -> None:
        # vision_shots given but none maps (all before this shot's midpoint window is fine — here
        # empty list path is None). Non-empty but silent-gate face with NO vision fill → heuristic.
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 50, 0.5, 0.16, -1.5)]
        # vision shot exists only far in the future; midpoint maps to nearest-first = that shot.
        # Use a fit vision shot so behaviour is deterministic + explicit.
        vis = plan_regions(
            [(0, 50)], tracks, fps=25.0, crop_w_frac=0.3167, vision_shots=[self._vs(0.0, "fit")]
        )
        assert vis[0].mode == "fit"

    def test_vision_fill_no_face_track_falls_through_to_fit(self) -> None:
        # vision says fill but there is NO face track at all → cannot crop to a subject → explicit
        # fallthrough to heuristics (which yield fit). Never a silent 0-region.
        from app.pipeline.stage3_reframe import plan_regions

        vis = plan_regions(
            [(0, 30)], [], fps=30.0, crop_w_frac=0.32, vision_shots=[self._vs(0.0, "fill", 0.5)]
        )
        assert vis[0].mode == "fit"

    def test_vision_ignored_under_explicit_mode_setting(self) -> None:
        # a global mode_setting override wins over vision (user forced it)
        from app.pipeline.stage3_reframe import plan_regions

        tracks = [self._track(0, 30, 0.5, 0.16, 0.9)]
        vis = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, mode_setting="fit",
            vision_shots=[self._vs(0.0, "fill", 0.5)],
        )  # fmt: skip
        assert vis[0].mode == "fit"


class TestComputeLockedCx:
    """compute_locked_cx: ОДИН статичный центр на шот, гарантирующий ВСЁ лицо в 9:16-кропе.

    Размах центров лица за шот + ширина лица должны влезть в ширину кропа — тогда ставим
    кроп на середину размаха (лицо целиком на КАЖДОМ кадре). Не влезает (субъект реально
    проходит через кадр) → None → вызывающий код фолбэчит на слежение (_track_trajectory).
    """

    def test_constant_center_returns_that_value(self) -> None:
        from app.pipeline.stage3_reframe import compute_locked_cx

        # лицо стоит на месте (cx=0.7) → лок ровно на нём
        assert compute_locked_cx([0.7, 0.7, 0.7], face_width=0.12, crop_w_frac=0.32) == 0.7

    def test_small_move_within_budget_locks_midpoint(self) -> None:
        from app.pipeline.stage3_reframe import compute_locked_cx

        # размах 0.10 + ширина 0.10 = 0.20 ≤ 0.32 → вмещается → центр = середина размаха
        cx = compute_locked_cx([0.40, 0.45, 0.50], face_width=0.10, crop_w_frac=0.32)
        assert cx is not None and abs(cx - 0.45) < 1e-9

    def test_move_exceeds_budget_returns_none(self) -> None:
        from app.pipeline.stage3_reframe import compute_locked_cx

        # размах 0.6 + ширина 0.1 = 0.7 > 0.32 → статикой лицо не удержать → None (фолбэк слежения)
        assert compute_locked_cx([0.2, 0.8], face_width=0.1, crop_w_frac=0.32) is None

    def test_full_crop_span_follows_with_margin(self) -> None:
        from app.pipeline.stage3_reframe import compute_locked_cx

        # размах 0.22 + ширина 0.10 = 0.32 == crop_w_frac: РАНЬШЕ локалось «край-в-край» (лицо
        # касалось границы = «пол-лица» на движении). Теперь с запасом _LOCK_COVERAGE_MARGIN=0.8
        # это > 0.32*0.8=0.256 → None → фолбэк на следование (лицо держится в центре).
        assert compute_locked_cx([0.39, 0.61], face_width=0.10, crop_w_frac=0.32) is None

    def test_locks_only_within_margin(self) -> None:
        # Робастно к значению _LOCK_COVERAGE_MARGIN: бюджет = crop_w_frac × margin.
        from app.pipeline.stage3_reframe import _LOCK_COVERAGE_MARGIN, compute_locked_cx

        crop, w = 0.40, 0.10
        budget = crop * _LOCK_COVERAGE_MARGIN
        span_ok = budget - w - 0.02  # span+width чуть НИЖЕ бюджета → лок с запасом от края
        span_bad = budget - w + 0.04  # чуть ВЫШЕ бюджета → None → следование (лицо не у края)
        ok = [0.5 - span_ok / 2, 0.5 + span_ok / 2]
        bad = [0.5 - span_bad / 2, 0.5 + span_bad / 2]
        assert compute_locked_cx(ok, face_width=w, crop_w_frac=crop) is not None
        assert compute_locked_cx(bad, face_width=w, crop_w_frac=crop) is None

    def test_empty_returns_none(self) -> None:
        from app.pipeline.stage3_reframe import compute_locked_cx

        assert compute_locked_cx([], face_width=0.1, crop_w_frac=0.32) is None


class TestPlanRegionsLock:
    """plan_regions с lock_per_shot (дефолт ON): containable fill-шот → ОДНА статичная точка
    (фокус зафиксирован на лице, камера не следует); субъект, реально проходящий через кадр
    дальше бюджета → фолбэк на слежение (много точек). lock_per_shot=False = старый follower."""

    def _moving(self, f0: int, f1: int, cx0: float, cx1: float, width: float, speak: float):  # type: ignore[no-untyped-def]
        from app.pipeline.stage3_reframe import SpeakerTrack

        n = f1 - f0
        cx = tuple(cx0 + (cx1 - cx0) * i / (n - 1) for i in range(n))
        return SpeakerTrack(f0=f0, f1=f1, cx=cx, width=width, speak=speak)

    def test_small_move_locks_to_single_point(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # лицо чуть водит 0.45→0.55 (размах 0.10 + ширина 0.10 = 0.20 ≤ 0.32) → ЛОК: одна точка
        tracks = [self._moving(0, 30, 0.45, 0.55, 0.10, 0.9)]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0)
        assert regions[0].mode == "fill"
        assert len(regions[0].points) == 1  # ЛОК, не траектория слежения
        assert abs(regions[0].points[0].cx - 0.5) < 1e-9  # на середине размаха

    def test_large_traverse_falls_back_to_follow(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # лицо реально идёт 0.15→0.85 (размах 0.70 > бюджета) → статикой не удержать → слежение
        tracks = [self._moving(0, 30, 0.15, 0.85, 0.10, 0.9)]
        regions = plan_regions([(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0)
        assert regions[0].mode == "fill"
        assert len(regions[0].points) > 1  # фолбэк: следует за движущимся, не теряет

    def test_lock_disabled_keeps_follower(self) -> None:
        from app.pipeline.stage3_reframe import plan_regions

        # lock_per_shot=False → старое поведение (follower) даже на малом движении
        tracks = [self._moving(0, 30, 0.45, 0.55, 0.10, 0.9)]
        regions = plan_regions(
            [(0, 30)], tracks, fps=30.0, crop_w_frac=0.32, smoothing=1.0, lock_per_shot=False
        )
        assert regions[0].mode == "fill"
        assert len(regions[0].points) > 1  # follower: несколько точек на движении


class TestDetectSceneCutsResourceRelease:
    """detect_scene_cuts ОБЯЗАН закрыть cv2.VideoCapture даже на ошибке detect_scenes.

    На Windows незакрытый VideoCapture держит файл-лок на temp seg.mp4 → выход из
    TemporaryDirectory роняет PermissionError, который МАСКИРУЕТ исходную JobError
    (и оставляет битый temp). Релиз должен быть в finally, не только на success-пути.
    """

    @staticmethod
    def _patch_ffmpeg_ok(monkeypatch: "pytest.MonkeyPatch") -> None:
        import app.pipeline.stage3_reframe as mod

        class _Proc:
            returncode = 0
            stderr = ""

        # ffmpeg-вырезка сегмента "успешна" (файл не нужен — scenedetect замокан)
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())

    def test_release_called_on_detect_failure(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        import scenedetect

        from app.pipeline.stage3_reframe import detect_scene_cuts

        self._patch_ffmpeg_ok(monkeypatch)
        released = {"n": 0}

        class _Cap:
            def release(self) -> None:
                released["n"] += 1

        class _Vid:
            capture = _Cap()

        class _SM:
            def add_detector(self, *_a: object) -> None: ...
            def detect_scenes(self, *_a: object) -> None:
                raise RuntimeError("decode boom")  # PySceneDetect упал в середине

            def get_scene_list(self) -> list:  # pragma: no cover
                return []

        monkeypatch.setattr(scenedetect, "open_video", lambda *_a: _Vid())
        monkeypatch.setattr(scenedetect, "SceneManager", lambda: _SM())
        monkeypatch.setattr(scenedetect, "ContentDetector", lambda **_k: object())

        with pytest.raises(JobError):
            detect_scene_cuts(__import__("pathlib").Path("x.mp4"), 0.0, 1.0, 25.0)
        assert released["n"] == 1  # capture закрыт несмотря на ошибку detect_scenes

    def test_release_called_on_success(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        import scenedetect

        from app.pipeline.stage3_reframe import detect_scene_cuts

        self._patch_ffmpeg_ok(monkeypatch)
        released = {"n": 0}

        class _Cap:
            def release(self) -> None:
                released["n"] += 1

        class _Vid:
            capture = _Cap()

        class _T:
            def __init__(self, f: int) -> None:
                self._f = f

            def get_frames(self) -> int:
                return self._f

        class _SM:
            def add_detector(self, *_a: object) -> None: ...
            def detect_scenes(self, *_a: object) -> None: ...
            def get_scene_list(self) -> list:
                return [(_T(0), _T(50)), (_T(50), _T(100))]  # одна склейка на кадре 50

        monkeypatch.setattr(scenedetect, "open_video", lambda *_a: _Vid())
        monkeypatch.setattr(scenedetect, "SceneManager", lambda: _SM())
        monkeypatch.setattr(scenedetect, "ContentDetector", lambda **_k: object())

        cuts = detect_scene_cuts(__import__("pathlib").Path("x.mp4"), 0.0, 1.0, 25.0)
        # 25fps = ЦЕЛЫЙ fps → _scene_cut_offset == 0 → склейка = start-кадр сцены 50 (РОВНО на
        # контент-cut). На дробном fps был бы 49 (-1, см. TestSceneCutOffset). Целое → Δ=0 цел.
        assert cuts == [50]
        assert released["n"] == 1


class TestMinSceneFrames:
    """LAYER 1: min_scene_len для ContentDetector привязан к НАТИВНОМУ fps, не к
    библиотечному дефолту 15 кадров (0.5с@30fps глотал быстрые реакционные склейки).

    0.25с лоупасс пропускает короткий рез на лицо, но душит саб-четвертьсекундный строб.
    Пол = 2 кадра (ContentDetector требует ≥1; 2 — минимальный осмысленный шот).
    """

    def test_quarter_second_grid_for_common_fps(self) -> None:
        from app.pipeline.stage3_reframe import _min_scene_frames

        assert _min_scene_frames(24.0, 0.25) == 6  # round(24*0.25)=6
        assert _min_scene_frames(25.0, 0.25) == 6  # round(25*0.25)=round(6.25)=6
        assert _min_scene_frames(29.97, 0.25) == 7  # round(29.97*0.25)=round(7.49)=7
        assert _min_scene_frames(60.0, 0.25) == 15  # round(60*0.25)=15

    def test_never_below_two_frames(self) -> None:
        from app.pipeline.stage3_reframe import _min_scene_frames

        # очень малый min_scene_sec / низкий fps → пол = 2 (ContentDetector требует ≥1)
        assert _min_scene_frames(25.0, 0.0) == 2
        assert _min_scene_frames(1.0, 0.25) == 2  # round(0.25)=0 → пол 2

    def test_below_library_default_for_30fps(self) -> None:
        from app.pipeline.stage3_reframe import _min_scene_frames

        # ключевой regression: 0.25с@30fps = 8 кадров < библиотечный дефолт 15 →
        # быстрый рез (≈8-14 кадров) выживает как своя сцена, а не дропается.
        assert _min_scene_frames(30.0, 0.25) == 8
        assert _min_scene_frames(30.0, 0.25) < 15


class TestDetectSceneCutsMinSceneLen:
    """detect_scene_cuts ОБЯЗАН передать min_scene_len (нативная сетка), а НЕ полагаться
    на библиотечный дефолт 15 кадров, который глотает короткие склейки до их детекции.
    """

    @staticmethod
    def _patch_ffmpeg_ok(monkeypatch: "pytest.MonkeyPatch") -> None:
        import app.pipeline.stage3_reframe as mod

        class _Proc:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())

    def test_passes_native_fps_min_scene_len_to_content_detector(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        import scenedetect

        from app.pipeline.stage3_reframe import detect_scene_cuts

        self._patch_ffmpeg_ok(monkeypatch)
        captured: dict[str, object] = {}

        class _Cap:
            def release(self) -> None: ...

        class _Vid:
            capture = _Cap()

        class _SM:
            def add_detector(self, *_a: object) -> None: ...
            def detect_scenes(self, *_a: object) -> None: ...
            def get_scene_list(self) -> list:
                return []

        def _spy_detector(**kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(scenedetect, "open_video", lambda *_a: _Vid())
        monkeypatch.setattr(scenedetect, "SceneManager", lambda: _SM())
        monkeypatch.setattr(scenedetect, "ContentDetector", _spy_detector)

        # 29.97fps, min_scene_sec=0.25 → round(29.97*0.25)=7, НЕ библиотечный дефолт 15.
        detect_scene_cuts(__import__("pathlib").Path("x.mp4"), 0.0, 1.0, 29.97, min_scene_sec=0.25)
        assert captured.get("min_scene_len") == 7
        assert captured.get("threshold") == 27.0  # порог не трогаем (H1 исключён)

    def test_min_scene_len_default_from_sec(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        import scenedetect

        from app.pipeline.stage3_reframe import detect_scene_cuts

        self._patch_ffmpeg_ok(monkeypatch)
        captured: dict[str, object] = {}

        class _Cap:
            def release(self) -> None: ...

        class _Vid:
            capture = _Cap()

        class _SM:
            def add_detector(self, *_a: object) -> None: ...
            def detect_scenes(self, *_a: object) -> None: ...
            def get_scene_list(self) -> list:
                return []

        def _spy_detector(**kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(scenedetect, "open_video", lambda *_a: _Vid())
        monkeypatch.setattr(scenedetect, "SceneManager", lambda: _SM())
        monkeypatch.setattr(scenedetect, "ContentDetector", _spy_detector)

        # без явного min_scene_sec → дефолт 0.4с @30fps = round(30*0.4)=12 (< библ. 15)
        detect_scene_cuts(__import__("pathlib").Path("x.mp4"), 0.0, 1.0, 30.0)
        assert captured.get("min_scene_len") == 12


class TestLockedCxForTracks:
    """locked_cx_for_tracks: лок-центр лица для РУЧНОГО tight-override (говорящий/крупнейший)."""

    def _track(self, f0: int, f1: int, cx: float, width: float, speak: float):  # type: ignore[no-untyped-def]
        from app.pipeline.stage3_reframe import SpeakerTrack

        return SpeakerTrack(f0=f0, f1=f1, cx=tuple([cx] * (f1 - f0)), width=width, speak=speak)

    def test_single_face_returns_its_center(self) -> None:
        from app.pipeline.stage3_reframe import locked_cx_for_tracks

        tracks = [self._track(0, 30, 0.7, 0.12, 0.9)]
        cx = locked_cx_for_tracks(tracks, 1280, 720, speak_threshold=0.0, smoothing=1.0)
        assert cx is not None and abs(cx - 0.7) < 1e-9

    def test_no_tracks_returns_none(self) -> None:
        from app.pipeline.stage3_reframe import locked_cx_for_tracks

        assert locked_cx_for_tracks([], 1280, 720, speak_threshold=0.0, smoothing=1.0) is None

    def test_picks_speaker_among_two(self) -> None:
        from app.pipeline.stage3_reframe import locked_cx_for_tracks

        # молчащий слева (0.3), говорящий справа (0.7) → лок на говорящего, не на центр/молчащего
        tracks = [self._track(0, 30, 0.3, 0.12, -0.5), self._track(0, 30, 0.7, 0.12, 0.9)]
        cx = locked_cx_for_tracks(tracks, 1280, 720, speak_threshold=0.0, smoothing=1.0)
        assert cx is not None and abs(cx - 0.7) < 1e-9


class TestArgmaxJump:
    """_argmax_jump: кадр с максимальным скачком контента (снап границы на реальную склейку)."""

    def test_picks_max_diff_frame(self) -> None:
        from app.pipeline.stage3_reframe import _argmax_jump

        # репро CARDI B: скачок 0.3→48.8 ровно на кадре 322 (истинная контент-склейка)
        diffs = [(320, 0.4), (321, 0.3), (322, 48.8), (323, 0.2)]
        assert _argmax_jump(diffs, fallback=999) == 322

    def test_empty_returns_fallback(self) -> None:
        from app.pipeline.stage3_reframe import _argmax_jump

        # нет данных (ffmpeg не дал кадров) → держим кандидат PySceneDetect, не ломаемся
        assert _argmax_jump([], fallback=321) == 321


class TestSceneCutOffset:
    """fps-зависимый сдвиг кадра склейки детектор→рендер (фикс 1-кадрового флеша на 25fps)."""

    def test_integer_fps_no_offset(self) -> None:
        from app.pipeline.stage3_reframe import _scene_cut_offset

        for fps in (24.0, 25.0, 30.0, 50.0, 60.0):
            assert _scene_cut_offset(fps) == 0, fps

    def test_fractional_fps_offset_one(self) -> None:
        from app.pipeline.stage3_reframe import _scene_cut_offset

        for fps in (23.976, 29.97, 59.94):
            assert _scene_cut_offset(fps) == 1, fps


class TestMergeShortRegionsFounderCut:
    """LAYER 2: при min_hold_sec=0.8 (новый дефолт) ~1с реакционный рез ВЫЖИВАЕТ как свой
    регион (юзер может тоглить шот), но 0.4с-микрорегион всё ещё гасится (анти-строб).
    """

    def test_one_second_region_survives_at_0_8_hold(self) -> None:
        # ~1с рез на лицо: при дефолте 1.5 он глотался предыдущим; при 0.8 — остаётся.
        regions = [
            TrackRegion(0.0, 3.0, "fit", ()),
            TrackRegion(3.0, 4.0, "fill", ()),  # 1.0с ≥ 0.8 → СВОЙ регион
            TrackRegion(4.0, 7.0, "fit", ()),
        ]
        result = merge_short_regions(regions, min_hold_sec=0.8)
        assert len(result) == 3
        assert result[1].mode == "fill"
        assert result[1].t0 == 3.0 and result[1].t1 == 4.0

    def test_sub_half_second_region_still_folded_at_0_8_hold(self) -> None:
        # 0.4с < 0.8 → всё ещё поглощается предыдущим (анти-строб пол держится).
        regions = [
            TrackRegion(0.0, 3.0, "fit", ()),
            TrackRegion(3.0, 3.4, "fill", ()),  # 0.4с < 0.8 → съеден
            TrackRegion(3.4, 7.0, "fit", ()),
        ]
        result = merge_short_regions(regions, min_hold_sec=0.8)
        assert len(result) == 2
        assert result[0].mode == "fit"
        assert result[0].t0 == 0.0 and result[0].t1 == 3.4


class TestRefineCutNativeCap:
    """_refine_cut_native ОБЯЗАН капить декод ffmpeg окном refine, а НЕ гнать до EOF.

    Без капа `-ss <start> -i <src> -vf select(...)` декодит от start до КОНЦА ИСХОДНИКА на
    КАЖДЫЙ кандидат склейки, оставляя ~9 PNG (на 86-мин сорсе = минуты на склейку; прод:
    ранние клипы ждали 491–776с в этой фазе). Кап `-frames:v (hi-lo+1)` — счётчик ВЫХОДНЫХ
    кадров: select пропускает ровно кадры n∈[lo,hi], значит лимит достигается ТОЛЬКО после
    кадра hi → декод глохнет сразу за окном, а селекция кадров БАЙТ-В-БАЙТ прежняя
    (-ss origin и select-выражение не тронуты → нумерация n та же).
    """

    @staticmethod
    def _capture_cmd(monkeypatch: "pytest.MonkeyPatch") -> dict:
        import app.pipeline.stage3_reframe as mod

        captured: dict = {}

        class _Proc:
            returncode = 0
            stderr = ""

        def _spy(cmd: list, *_a: object, **_k: object) -> "_Proc":
            captured["cmd"] = list(cmd)
            return _Proc()

        monkeypatch.setattr(mod.subprocess, "run", _spy)
        return captured

    def test_decode_capped_by_frames_flag_not_eof(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        from pathlib import Path

        from app.pipeline.stage3_reframe import _refine_cut_native

        captured = self._capture_cmd(monkeypatch)
        got = _refine_cut_native(Path("x.mp4"), 12.0, 300, window=4)
        cmd = captured["cmd"]
        assert "-frames:v" in cmd  # декод ограничен окном, не до EOF
        # окно: lo=296, hi=304 → select отдаёт максимум hi-lo+1=9 кадров. Кап РАВЕН этому
        # числу: покрывает ВСЁ окно (остановиться раньше кадра hi лимит физически не может)
        # и конечен. ⚠️ Запас (+margin) счётчику ПРОТИВОПОКАЗАН: лимит стал бы недостижим
        # (select больше кадров не отдаст) → декод снова ушёл бы в EOF.
        assert cmd[cmd.index("-frames:v") + 1] == "9"
        # кап — выходная опция (после -i), иначе ffmpeg трактует её как входную
        assert cmd.index("-frames:v") > cmd.index("-i")
        assert got == 300  # мок не дал PNG → fail-safe кандидат (поведение не тронуто)

    def test_ss_and_select_expr_byte_identical(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        # Нумерация n считается от -ss-origin: seek и select-выражение обязаны остаться
        # ровно прежними, иначе выбираются ДРУГИЕ кадры (сломан снап границы = флеш).
        from pathlib import Path

        from app.pipeline.stage3_reframe import _refine_cut_native

        captured = self._capture_cmd(monkeypatch)
        _refine_cut_native(Path("v.mp4"), 3.5, 100, window=4)
        cmd = captured["cmd"]
        assert cmd[cmd.index("-ss") + 1] == "3.5"
        assert cmd.index("-ss") < cmd.index("-i")  # input-seek: n=0 на seek-точке
        assert cmd[cmd.index("-vf") + 1] == "select='gte(n\\,96)*lte(n\\,104)'"
        assert cmd[cmd.index("-start_number") + 1] == "96"

    def test_cap_respects_lo_floor_near_segment_start(
        self, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        # Кандидат у самого старта: lo = max(1, 2-4) = 1, hi = 6 → окно 6 кадров, кап = 6.
        from pathlib import Path

        from app.pipeline.stage3_reframe import _refine_cut_native

        captured = self._capture_cmd(monkeypatch)
        _refine_cut_native(Path("v.mp4"), 0.0, 2, window=4)
        cmd = captured["cmd"]
        assert cmd[cmd.index("-vf") + 1] == "select='gte(n\\,1)*lte(n\\,6)'"
        assert cmd[cmd.index("-frames:v") + 1] == "6"


class TestReframeTimingLine:
    """reframe_segment печатает ОДНУ компактную строку фазовых таймингов на клип.

    Прод-лог между стартом контейнера и финальной clip-строкой иначе ПУСТ — тюнить перф
    (scene_detect/refine/sample/detect/asd) приходится вслепую. Строка одна, не per-frame.
    """

    def test_prints_single_timing_line_with_all_phases(
        self,
        monkeypatch: "pytest.MonkeyPatch",
        capsys: "pytest.CaptureFixture[str]",
        tmp_path: Path,
    ) -> None:
        import app.pipeline.asd_reframe as asd_mod
        import app.pipeline.stage3_reframe as mod

        monkeypatch.setattr(mod, "detect_scene_cuts", lambda *a, **k: [30])
        monkeypatch.setattr(asd_mod, "score_tracks_in_segment", lambda *a, **k: [])
        regions, face_found = mod.reframe_segment(
            tmp_path / "src.mp4",
            1920,
            1080,
            0.0,
            2.0,
            clip_id="clip_00",
            out_dir=tmp_path,
            fps=30.0,
        )
        assert face_found is False and regions  # поведение не тронуто
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[reframe]")]
        assert len(lines) == 1  # одна строка на клип, не спам
        for field in (
            "clip=clip_00",
            "scene_detect=",
            "cuts=1",
            "refine=",
            "sample=",
            "detect=",
            "asd=",
            "tracks=0",
            "total=",
        ):
            assert field in lines[0], f"missing {field!r} in {lines[0]!r}"


class TestResampleTrack:
    """resample_track: дорожка ASD@25fps → нативная сетка fps (фикс флеша на ≠25fps видео).

    Корень бага: геометрия регионов была в сетке face_fps=25, а рендер режет в нативном
    fps источника (29.97/23.976/...). round(t0*native_fps) уезжал на ±1 кадр от склейки →
    один кадр нового шота со старым кропом = флеш. На 25fps-видео (comedy01/dod01) сетки
    совпадали → баг был невидим в тестах.
    """

    @staticmethod
    def _track(f0: int, f1: int, cx: float) -> object:
        from app.pipeline.stage3_reframe import SpeakerTrack

        return SpeakerTrack(f0=f0, f1=f1, cx=tuple([cx] * (f1 - f0)), width=0.1, speak=0.9)

    def test_identity_when_same_fps(self) -> None:
        from app.pipeline.stage3_reframe import resample_track

        t = self._track(0, 25, 0.4)
        assert resample_track(t, 25.0, 25.0) is t

    def test_25_to_30_upsamples_frame_count(self) -> None:
        from app.pipeline.stage3_reframe import resample_track

        # 1с дорожки @25fps (кадры 0..25) → @30fps (кадры 0..30)
        t = self._track(0, 25, 0.6)
        r = resample_track(t, 25.0, 30.0)
        assert r.f0 == 0 and r.f1 == 30
        assert len(r.cx) == 30
        assert all(abs(c - 0.6) < 1e-9 for c in r.cx)  # постоянный cx сохраняется
        assert r.width == t.width and r.speak == t.speak  # скаляры не трогаем

    def test_25_to_2997_offset_track(self) -> None:
        from app.pipeline.stage3_reframe import resample_track

        # дорожка не с нуля: f0=50@25fps (t=2.0с) → 2.0*29.97≈59.94 → кадр 60
        t = self._track(50, 75, 0.3)
        r = resample_track(t, 25.0, 29.97)
        assert r.f0 == round(50 / 25 * 29.97)  # == 60
        assert r.f1 == round(75 / 25 * 29.97)  # == 90
        assert len(r.cx) == r.f1 - r.f0  # инвариант: len(cx) == f1-f0

    def test_native_grid_boundaries_land_on_exact_render_frames(self) -> None:
        """ГЛАВНЫЙ инвариант фикса: t1 региона в нативной сетке → round(t1*fps) точный кадр."""
        from app.pipeline.stage3_reframe import resample_track

        for native_fps in (23.976, 24.0, 29.97, 60.0):
            t = self._track(0, 25, 0.5)
            r = resample_track(t, 25.0, native_fps)
            t1_sec = r.f1 / native_fps
            assert round(t1_sec * native_fps) == r.f1  # рендер режет ровно на границе → нет флеша

    def test_resampled_cx_tracks_movement(self) -> None:
        from app.pipeline.stage3_reframe import SpeakerTrack, resample_track

        # cx растёт 0.0→1.0 по 25 кадрам; после ресемпла в 50fps движение монотонно сохраняется
        cx = tuple(i / 24 for i in range(25))
        t = SpeakerTrack(f0=0, f1=25, cx=cx, width=0.1, speak=0.5)
        r = resample_track(t, 25.0, 50.0)
        assert r.cx[0] < r.cx[len(r.cx) // 2] < r.cx[-1]


class TestMergeShortCutaway:
    """merge_short_regions + crop_w_frac: короткую перебивку-реакцию (реальный отдельный шот) НЕ
    проглатываем в предыдущий fill, если удержание его кропа промахнётся мимо нового субъекта →
    иначе плечо (Cardi B clip_04 @40.5с: шот 40.0–40.9с cx 0.64 держал кроп Карди 0.42)."""

    def _fill(self, t0: float, t1: float, cx: float):  # type: ignore[no-untyped-def]
        from app.pipeline.stage3_reframe import TrackPoint, TrackRegion

        return TrackRegion(
            t0=t0, t1=t1, mode="fill", points=(TrackPoint(t=t0, mode="fill", cx=cx),), points_b=()
        )

    def test_keeps_short_distant_cutaway(self) -> None:
        from app.pipeline.stage3_reframe import merge_short_regions

        # Карди 0.42 → перебивка интервьюера 0.9с cx 0.64 → Карди 0.43. |0.64-0.42|=0.22 > пол-кропа
        # (0.316*0.5=0.158) → перебивку НЕ глотаем (иначе плечо), она держит СВОЙ кроп 0.64.
        regions = [
            self._fill(27.0, 40.0, 0.42),
            self._fill(40.0, 40.9, 0.64),
            self._fill(40.9, 48.6, 0.43),
        ]
        merged = merge_short_regions(regions, 1.5, crop_w_frac=0.316)
        assert [round(r.t0, 2) for r in merged] == [27.0, 40.0, 40.9]  # перебивка выжила
        assert round(merged[1].points[0].cx, 2) == 0.64  # свой кроп, не Карди

    def test_keeps_mid_distance_cutaway(self) -> None:
        from app.pipeline.stage3_reframe import merge_short_regions

        # Cardi B clip_06: короткая перебивка интервьюера cx 0.59 vs Карди 0.44, Δ=0.15. При старом
        # пороге 0.5*crop(0.158) глоталась → пол-лица срезано. При 0.33*crop(0.104): 0.15 > 0.104 →
        # держит СВОЙ кроп 0.59 (лицо интервьюера целиком).
        regions = [
            self._fill(0.0, 13.0, 0.44),
            self._fill(13.0, 14.1, 0.59),
            self._fill(14.1, 20.0, 0.44),
        ]
        merged = merge_short_regions(regions, 1.5, crop_w_frac=0.316)
        assert [round(r.t0, 2) for r in merged] == [0.0, 13.0, 14.1]  # перебивка выжила
        assert round(merged[1].points[0].cx, 2) == 0.59

    def test_swallows_short_near_cutaway(self) -> None:
        from app.pipeline.stage3_reframe import merge_short_regions

        # Тот же короткий шот, но субъект БЛИЗКО (0.42→0.47, Δ=0.05 < 0.158): держащийся кроп его
        # вмещает → глотаем как прежде (анти-строб, лишний шот не плодим).
        regions = [
            self._fill(27.0, 40.0, 0.42),
            self._fill(40.0, 40.9, 0.47),
            self._fill(40.9, 48.6, 0.43),
        ]
        merged = merge_short_regions(regions, 1.5, crop_w_frac=0.316)
        assert [round(r.t0, 2) for r in merged] == [27.0, 40.9]  # средний свёрнут

    def test_no_crop_w_frac_swallows_all_short(self) -> None:
        from app.pipeline.stage3_reframe import merge_short_regions

        # crop_w_frac=None (kill-switch off) → прежнее поведение: любой короткий шот проглочен.
        regions = [
            self._fill(27.0, 40.0, 0.42),
            self._fill(40.0, 40.9, 0.64),
            self._fill(40.9, 48.6, 0.43),
        ]
        merged = merge_short_regions(regions, 1.5)
        assert [round(r.t0, 2) for r in merged] == [27.0, 40.9]  # средний свёрнут (как раньше)

    def test_keeps_short_fit_cutaway_after_fill(self) -> None:
        from app.pipeline.stage3_reframe import TrackRegion, merge_short_regions

        # Молчащий интервьюер → silence-гейт делает перебивку FIT. Держать тайт-кроп Карди (fill)
        # поверх широкого fit-контента = плечо (баг Cardi B clip_02) → короткий fit после fill НЕ
        # глотаем, он показывается wide (свой fit). Это НЕ трогает fit↔fit анти-строб.
        regions = [
            self._fill(0.0, 13.0, 0.42),
            TrackRegion(t0=13.0, t1=13.5, mode="fit", points=(), points_b=()),
            self._fill(13.5, 20.0, 0.43),
        ]
        merged = merge_short_regions(regions, 1.5, crop_w_frac=0.316)
        assert [round(r.t0, 2) for r in merged] == [0.0, 13.0, 13.5]  # fit-перебивка выжила
        assert merged[1].mode == "fit"

    def test_subfloor_cutaway_still_swallowed(self) -> None:
        from app.pipeline.stage3_reframe import merge_short_regions

        # Ультра-короткий обрезок (0.16с < 0.4с порога) на границе клипа — даже далёкий fill глотаем
        # (лишний wide-блик заметнее ~5 кадров чужого кропа).
        regions = [
            self._fill(0.0, 13.0, 0.42),
            self._fill(13.0, 13.16, 0.64),
        ]
        merged = merge_short_regions(regions, 1.5, crop_w_frac=0.316)
        assert [round(r.t0, 2) for r in merged] == [0.0]  # обрезок свёрнут

    def test_short_fit_after_fit_still_swallowed(self) -> None:
        from app.pipeline.stage3_reframe import TrackRegion, merge_short_regions

        # prev = fit (полный кадр, кропом не промахнёшься) → короткий fit глотается как прежде
        # (анти-строб fit↔fit не трогаем — плеча тут быть не может).
        regions = [
            TrackRegion(t0=0.0, t1=13.0, mode="fit", points=(), points_b=()),
            TrackRegion(t0=13.0, t1=13.5, mode="fit", points=(), points_b=()),
            self._fill(13.5, 20.0, 0.43),
        ]
        merged = merge_short_regions(regions, 1.5, crop_w_frac=0.316)
        assert [round(r.t0, 2) for r in merged] == [0.0, 13.5]  # средний fit свёрнут в fit
