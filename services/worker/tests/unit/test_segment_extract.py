"""Unit-тесты посегментной нарезки (per-clip segment extraction).

Ядро — ЧИСТАЯ математика ре-якорения сегмента на новый origin (кейфрейм) и ДОКАЗАТЕЛЬСТВО
инвариантности кадровой сетки (Δ=0): рендер-контейнер получает не весь source, а маленький
stream-copy сегмент от кейфрейма ≤ seg.start; координаты клипа сдвигаются на keyframe_time =
K/fps (K — ЦЕЛЫЙ индекс кадра). Тогда `round(shifted*fps) == round(seg.start*fps) − K` ТОЧНО →
reframe/render (НЕизменённые) режут ровно те же кадры источника → флешей нет, качество 1:1.

⚠️ Читай docs/REFRAME_FPS_GRID_INVARIANT.md перед правкой. Тест ниже — тот же вид проверки,
что `test_flatten_frame_grid_invariant_across_fps` (test_timeline_filter.py), но для сдвига на
кейфрейм-origin. МАТРИЦА fps обязательна (баг невидим на 25fps — см. инвариант-доку).
"""

from __future__ import annotations

import pytest

from app.models import Segment
from app.pipeline import segment_extract as se

# fleet native fps, включая дробные (где сетка ломалась) — как в инвариант-тестах
_FLEET_FPS = [23.976, 24.0, 25.0, 29.97, 30.0, 59.94, 60.0]


def _seg(start: float, end: float) -> Segment:
    return Segment(
        start=start,
        end=end,
        reason="r",
        score=0.5,
        type="hook",
        hook="h",
        why_works="w",
        hook_style="pov",
        tone="funny",
        key_quote="q",
        energy=0.7,
    )


# ── parse_keyframe_pts ─────────────────────────────────────────────────────
def test_parse_keyframe_pts_keeps_only_keyframe_packets() -> None:
    # ffprobe -show_entries packet=pts_time,flags -of csv=p=0 → "pts,flags" per line.
    stdout = "\n".join(
        [
            "0.000000,K__",  # keyframe
            "0.033367,___",  # non-key
            "0.066733,___",
            "2.002000,K__",  # keyframe
            "N/A,K__",  # keyframe but no pts → skip
            "",  # blank → skip
            "4.004000,K_",  # keyframe (short flags)
        ]
    )
    assert se.parse_keyframe_pts(stdout) == [0.0, 2.002, 4.004]


def test_parse_keyframe_pts_sorts_and_handles_empty() -> None:
    assert se.parse_keyframe_pts("") == []
    assert se.parse_keyframe_pts("5.0,K\n1.0,K\n3.0,___") == [1.0, 5.0]


# ── preceding_keyframe ─────────────────────────────────────────────────────
def test_preceding_keyframe_largest_at_or_before() -> None:
    kf = [0.0, 2.0, 4.0, 6.0]
    assert se.preceding_keyframe(kf, 5.3) == 4.0  # between 4 and 6 → 4
    assert se.preceding_keyframe(kf, 4.0) == 4.0  # exact match
    assert se.preceding_keyframe(kf, 0.0) == 0.0
    assert se.preceding_keyframe(kf, 100.0) == 6.0  # after all → last


def test_preceding_keyframe_before_all_falls_back_to_zero() -> None:
    # t before the earliest keyframe (degenerate; frame 0 is normally a keyframe) → 0.0 (safe:
    # 0 <= any seg.start). NEVER return a keyframe AFTER t (would drop clip content).
    assert se.preceding_keyframe([2.0, 4.0], 1.0) == 0.0
    assert se.preceding_keyframe([], 3.0) == 0.0


# ── snap_to_frame ──────────────────────────────────────────────────────────
def test_snap_to_frame_rounds_pts_to_exact_frame_boundary() -> None:
    # ffprobe pts is microsecond-rounded; snap it back to the exact K/fps grid.
    for fps in _FLEET_FPS:
        for k in [0, 1, 50, 300]:
            pts_approx = round(k / fps, 6)  # what ffprobe reports
            snapped = se.snap_to_frame(pts_approx, fps)
            assert abs(snapped - k / fps) < 1e-12
            assert round(snapped * fps) == k  # lands on frame K exactly


# ── shift_segment ──────────────────────────────────────────────────────────
def test_shift_segment_shifts_bounds_preserves_all_other_fields() -> None:
    seg = _seg(100.0, 130.0)
    out = se.shift_segment(seg, 98.5, 30.0)  # kt=98.5 = frame 2955; a=3000 → new_start=45/30=1.5
    assert out.start == pytest.approx(1.5)
    assert out.end == pytest.approx(31.5)  # span 130−100=30 carried
    # every other field preserved (regression against model_copy dropping fields)
    assert out.reason == seg.reason
    assert out.score == seg.score
    assert out.type == seg.type
    assert out.hook == seg.hook
    assert out.why_works == seg.why_works
    assert out.hook_style == seg.hook_style
    assert out.tone == seg.tone
    assert out.key_quote == seg.key_quote
    assert out.energy == seg.energy


def test_shift_segment_zero_keyframe_snaps_start_to_frame() -> None:
    # kt=0 → NOT raw identity: start is pre-snapped to its frame grid ((a-0)/fps) — exactly what
    # reframe does internally anyway (aligned_start). Same start FRAME, span preserved.
    seg = _seg(12.34, 45.6)
    out = se.shift_segment(seg, 0.0, 30.0)
    assert round(out.start * 30.0) == round(seg.start * 30.0)  # same start frame
    assert out.end - out.start == pytest.approx(seg.end - round(seg.start * 30.0) / 30.0)


# ── THE Δ=0 PROOF (formal, executable): shifting to a keyframe origin lands on
#    the SAME absolute source frames the full-source path would cut. ──────────
@pytest.mark.parametrize("fps", _FLEET_FPS)
@pytest.mark.parametrize("seg_start", [0.146, 4.788, 17.35, 33.337, 612.4, 5114.2])
@pytest.mark.parametrize("k_idx", [0, 7, 50, 101, 499, 3000])
def test_shift_preserves_native_frame_grid(fps: float, seg_start: float, k_idx: int) -> None:
    keyframe_time = se.snap_to_frame(round(k_idx / fps, 6), fps)  # exact K/fps as prod computes it
    if keyframe_time > seg_start:
        pytest.skip("keyframe after seg_start is not a valid preceding keyframe")
    shifted = se.shift_segment(_seg(seg_start, seg_start + 20.0), keyframe_time, fps)
    # frame the SEGMENT path cuts (round(new_start*fps)) == frame the FULL-SOURCE path cuts, for
    # EVERY fps incl. the 17.35@30 half-frame case that the naive shift got wrong. Exact identity.
    assert round(shifted.start * fps) == round(seg_start * fps) - k_idx


def test_naive_shift_breaks_at_half_frame_but_shift_segment_fixes_it() -> None:
    # DOCUMENTS the banker's-rounding gotcha the blueprint's real-number algebra missed.
    fps, seg_start, k_idx = 30.0, 17.35, 499
    kt = k_idx / fps
    naive = round((seg_start - kt) * fps)  # 22 (round(21.5)→even)
    correct = round(seg_start * fps) - k_idx  # 520-499 = 21
    assert naive != correct  # the naive `seg.start - kt` would be 1 frame off here
    fixed = round(se.shift_segment(_seg(seg_start, seg_start + 20.0), kt, fps).start * fps)
    assert fixed == correct  # shift_segment's pre-snap lands on the right frame


# ── probe_window / extraction_end (clamping) ───────────────────────────────
def test_probe_window_clamped() -> None:
    assert se.probe_window(100.0, lookback_sec=30.0, duration=200.0) == (70.0, 100.0)
    assert se.probe_window(10.0, lookback_sec=30.0, duration=200.0) == (0.0, 10.0)  # no negative
    assert se.probe_window(250.0, lookback_sec=30.0, duration=200.0) == (220.0, 200.0)  # hi<=dur


def test_extraction_end_adds_margin_clamped_to_duration() -> None:
    assert se.extraction_end(120.0, trailing_margin_sec=2.0, duration=300.0) == 122.0
    assert se.extraction_end(299.5, trailing_margin_sec=2.0, duration=300.0) == 300.0  # clamp
