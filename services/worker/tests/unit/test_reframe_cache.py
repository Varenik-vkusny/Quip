"""Tests for split-capable, frame-snapped override application in reframe_cache.

Covers the NEW behavior of `apply_overrides_to_regions` (domain: force-Tight/Wide
override that SPLITS a region at a frame-snapped boundary so the user can recolor an
arbitrary sub-range — a shot the detector/merge fused away). The old behavior only
recolored whole regions by midpoint and could never re-cut, so a missing boundary was
unrecoverable.

CRITICAL INVARIANT (REFRAME_FPS_GRID_INVARIANT.md): every split boundary MUST be
frame-snapped to the source NATIVE fps grid — t = round(t*fps)/fps — so render's
trim=start_frame=round(t0*fps) lands on an exact native frame (Δ=0, no flash on ≠25fps).
"""

from app.editor.reframe_cache import apply_overrides_to_regions
from app.models import CropOverride, SourceInterval
from app.pipeline.stage3_reframe import TrackPoint, TrackRegion

FPS_2997 = 30000.0 / 1001.0  # 29.97 — the classic ≠25fps case the invariant guards


# Sub-nanoframe tolerance: a snapped boundary t=round(x)/fps satisfies round(t*fps)==x, so the
# render trim (start_frame=round(t0*fps)) lands on the EXACT native frame. With exact-rational fps
# (30000/1001) the float roundtrip leaves ~1e-15 residue — well under any frame impact (DoD: <0.01).
_FRAME_EPS = 1e-9


def _frame_error(t: float, fps: float) -> float:
    """|t*fps - round(t*fps)| — ~0 means the boundary lands on an exact native frame."""
    return abs(t * fps - round(t * fps))


def _three_shots() -> list[TrackRegion]:
    # interval-relative: shot#1 [0,2) fill, shot#2 [2,4) fill, shot#3 [4,6) fit
    return [
        TrackRegion(t0=0.0, t1=2.0, mode="fill", points=(TrackPoint(t=0.0, mode="fill", cx=0.4),)),
        TrackRegion(t0=2.0, t1=4.0, mode="fill", points=(TrackPoint(t=2.0, mode="fill", cx=0.6),)),
        TrackRegion(t0=4.0, t1=6.0, mode="fit", points=()),
    ]


def test_partial_override_splits_region_into_three_frame_snapped():
    # (a) A partial override STRICTLY inside a single region splits it into 3 pieces;
    # every resulting boundary is frame-snapped to the source native fps (29.97).
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    # One big fill region whose own boundaries are ALREADY on the native frame grid (exactly what
    # reframe_segment emits: t = cut_frame/fps). Frame 0 .. frame 180 at 29.97.
    region_end = 180 / FPS_2997  # ≈ 6.006s — on the grid
    regions = [
        TrackRegion(
            t0=0.0, t1=region_end, mode="fill", points=(TrackPoint(t=0.0, mode="fill", cx=0.4),)
        )
    ]
    # Override covers an arbitrary mid sub-range (source 11.4..14.3, i.e. interval-relative
    # 1.4..4.3) — NOT aligned to any region boundary and NOT on the fps grid.
    ov = [CropOverride(source_start=11.4, source_end=14.3, mode="fit")]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)

    assert len(out) == 3, "partial override must split the region into 3 pieces"
    # piece order + modes: keep | override | keep
    assert [r.mode for r in out] == ["fill", "fit", "fill"]
    # outer pieces retain the original mode AND points; outer edges = region's own (snapped) edges
    assert out[0].t0 == 0.0 and out[0].points == regions[0].points
    assert out[2].t1 == region_end and out[2].points == regions[0].points
    # contiguous, no gaps/overlaps
    assert out[0].t1 == out[1].t0
    assert out[1].t1 == out[2].t0
    # middle piece is the override mode with no fill trajectory (fit)
    assert out[1].mode == "fit" and out[1].points == ()
    # EVERY boundary is frame-snapped (Δ frame == 0) — the whole point of this change
    for r in out:
        assert _frame_error(r.t0, FPS_2997) < _FRAME_EPS, f"unsnapped t0={r.t0}"
        assert _frame_error(r.t1, FPS_2997) < _FRAME_EPS, f"unsnapped t1={r.t1}"
    # the inner boundaries are the snapped override edges (rel 1.4 / 4.3)
    a = round(1.4 * FPS_2997) / FPS_2997
    b = round(4.3 * FPS_2997) / FPS_2997
    assert out[1].t0 == a
    assert out[1].t1 == b


def test_whole_region_override_recolors_without_split():
    # (b) An override fully covering a region recolors it in place — NO split, same
    # boundaries, same count. Proves backward-compat (existing clips/tests unaffected).
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    regions = _three_shots()
    # override fully covers shot#2 [2,4) (source 12..14)
    ov = [CropOverride(source_start=12.0, source_end=14.0, mode="fit")]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)

    assert len(out) == 3, "whole-region override must NOT split — same region count"
    assert (out[0].t0, out[0].t1, out[0].mode) == (0.0, 2.0, "fill")
    assert out[0].points == regions[0].points
    assert (out[1].t0, out[1].t1, out[1].mode) == (2.0, 4.0, "fit")  # recolored, boundaries kept
    assert out[1].points == ()
    assert (out[2].t0, out[2].t1, out[2].mode) == (4.0, 6.0, "fit")
    assert out[2].points == regions[2].points


def test_override_on_exact_boundary_no_zero_length_slivers():
    # (c) An override whose edges coincide with region boundaries must not produce
    # zero/negative-length pieces. Override = exactly shot#2 [2,4) (source 12..14).
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    regions = _three_shots()
    ov = [CropOverride(source_start=12.0, source_end=14.0, mode="fit")]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)

    # exactly 3 regions — no slivers from the touching boundaries of shot#1 / shot#3
    assert len(out) == 3
    for r in out:
        assert r.t1 > r.t0, f"zero/negative-length sliver: ({r.t0}, {r.t1})"
    # shot#2 fully covered → recolored (not re-cut)
    assert out[1].mode == "fit"
    assert (out[1].t0, out[1].t1) == (2.0, 4.0)
    # shots #1/#3 untouched (no slivers introduced)
    assert out[0] == regions[0]
    assert out[2] == regions[2]


def test_partial_override_last_wins_across_overlap():
    # last-wins: when two overrides hit the SAME region, the LAST one defines the recolor.
    # Here the override partially overlaps a single fill region → split, middle uses the
    # LAST override's mode/center.
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    regions = [
        TrackRegion(t0=0.0, t1=6.0, mode="fill", points=(TrackPoint(t=0.0, mode="fill", cx=0.4),))
    ]
    ov = [
        CropOverride(source_start=12.0, source_end=14.0, mode="fit"),
        CropOverride(source_start=12.0, source_end=14.0, mode="fill", center=0.9),
    ]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)
    # middle piece (source 12..14 → rel 2..4, both interior to [0,6)) recolored by the LAST
    # override. Its boundaries are frame-snapped to the 29.97 grid (rel 2.0/4.0 are NOT on it).
    mid = [r for r in out if r.mode == "fill" and r.points and r.points[0].cx == 0.9]
    assert len(mid) == 1, "last override must win on the covered sub-range"
    # the recolored sub-range's (interior, new) boundaries are frame-snapped to the native grid
    assert mid[0].t0 == round(2.0 * FPS_2997) / FPS_2997
    assert mid[0].t1 == round(4.0 * FPS_2997) / FPS_2997
    assert _frame_error(mid[0].t0, FPS_2997) < _FRAME_EPS
    assert _frame_error(mid[0].t1, FPS_2997) < _FRAME_EPS


def test_multiple_overrides_on_one_region_each_applies():
    # Regression: a single "last override wins for the WHOLE region" would silently DROP every
    # edit but the last. The editor "Split here" flow emits exactly this — two NON-overlapping
    # sub-range overrides on one detector-fused region — so BOTH must survive (each its own mode).
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    region_end = 180 / FPS_2997  # on the 29.97 grid (cut_frame/fps), like reframe_segment emits
    regions = [
        TrackRegion(
            t0=0.0, t1=region_end, mode="fill", points=(TrackPoint(t=0.0, mode="fill", cx=0.5),)
        )
    ]
    ov = [
        CropOverride(source_start=10.5, source_end=11.5, mode="fit"),  # rel 0.5..1.5
        CropOverride(source_start=13.0, source_end=14.0, mode="fill", center=0.9),  # rel 3.0..4.0
    ]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)
    modes = [r.mode for r in out]
    assert "fit" in modes, "the FIRST override must not be dropped by the second"
    fill09 = [r for r in out if r.mode == "fill" and r.points and r.points[0].cx == 0.9]
    assert len(fill09) == 1, "the SECOND, non-overlapping override must also apply"
    # contiguous tiling of the original region, every boundary frame-snapped (no flash)
    assert out[0].t0 == 0.0 and out[-1].t1 == region_end
    for prev, nxt in zip(out, out[1:], strict=False):
        assert prev.t1 == nxt.t0  # no gaps/overlaps
    for r in out:
        assert _frame_error(r.t0, FPS_2997) < _FRAME_EPS, f"unsnapped t0={r.t0}"
        assert _frame_error(r.t1, FPS_2997) < _FRAME_EPS, f"unsnapped t1={r.t1}"


# ── Follow-mode manual-pan authority (bug fix 2026-07-11) ────────────────────────
# Repro: user forces a shot to tight (fill) + a manual crop center (pan), then turns Follow ON
# (lock_per_shot=False). reframe_segment rebuilds the shot as a MULTI-POINT follower; merge/boundary
# drift vs the Lock layout the override was captured against leaves the fill override a few frames
# short of the follow region → apply_overrides_to_regions SPLITS the region and a KEPT follower
# (multi-point) fill sliver survives next to the static-center piece. The follower re-tracks the
# face and the render clamps cx to the frame edge → the manual pan is ignored (crop jumps right).
# DESIGN DECISION: a manual fill override OWNS the whole shot statically — Follow never re-tracks an
# overridden shot → any follower sliver of a fill-overridden region is absorbed into the static
# center. Only mode/points change; t0/t1 stay frame-snapped (Δ=0, REFRAME_FPS_GRID_INVARIANT).


def _follow_region(t1: float, cxs=(0.4, 0.6, 0.8, 0.95)) -> TrackRegion:
    """A fill region with a MULTI-POINT follower trajectory (what Follow / _track_trajectory emits).

    cx rises across the shot (tracking a face drifting right) — the exact case whose tail the render
    would clamp to the frame edge if it survived next to a manual static pin.
    """
    n = len(cxs)
    pts = tuple(TrackPoint(t=(i / (n - 1)) * t1, mode="fill", cx=cx) for i, cx in enumerate(cxs))
    return TrackRegion(t0=0.0, t1=t1, mode="fill", points=pts)


def test_follow_fill_override_absorbs_follower_sliver():
    # Single fill override center=0.6 that does NOT exactly span the multi-point follow region
    # (a few frames short on each side, simulating follow-boundary drift). Expect: NO surviving
    # multi-point follower fill piece and EVERY fill piece pinned to the static center 0.6.
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    region_end = 180 / FPS_2997  # ≈6.006s, on the native grid (cut_frame/fps), like reframe_segment
    regions = [_follow_region(region_end)]
    # override short of the region on BOTH sides (rel 0.2..5.8 vs region [0, ~6.006))
    ov = [CropOverride(source_start=10.2, source_end=15.8, mode="fill", center=0.6)]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)

    # the follower is dead: no fill piece keeps a multi-point trajectory
    assert all(not (r.mode == "fill" and len(r.points) > 1) for r in out), (
        "a follower (multi-point) fill sliver survived next to the manual pin"
    )
    # every fill portion uses the static manual center 0.6
    fill_pieces = [r for r in out if r.mode == "fill"]
    assert fill_pieces, "the overridden shot must stay fill"
    for r in fill_pieces:
        assert len(r.points) == 1 and r.points[0].cx == 0.6, f"non-static/off-center piece: {r}"
    # boundaries untouched: contiguous tiling of the ORIGINAL region, every edge frame-snapped (Δ=0)
    assert out[0].t0 == 0.0 and out[-1].t1 == region_end
    for prev, nxt in zip(out, out[1:], strict=False):
        assert prev.t1 == nxt.t0
    for r in out:
        assert _frame_error(r.t0, FPS_2997) < _FRAME_EPS, f"unsnapped t0={r.t0}"
        assert _frame_error(r.t1, FPS_2997) < _FRAME_EPS, f"unsnapped t1={r.t1}"


def test_follow_fill_override_keeps_fit_sliver_and_other_override():
    # Same follow region, but a fit override also covers a sub-range. The fit sub-range must STAY
    # fit (a piece covered by a DIFFERENT override keeps that override), while the follower slivers
    # around the fill pin are still absorbed to the static center — fit is never touched.
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    region_end = 180 / FPS_2997
    regions = [_follow_region(region_end)]
    ov = [
        CropOverride(source_start=10.2, source_end=15.8, mode="fill", center=0.6),  # rel 0.2..5.8
        CropOverride(source_start=12.0, source_end=13.0, mode="fit"),  # rel 2..3, last → wins there
    ]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)

    # the fit sub-range survives as a fit piece
    fit_pieces = [r for r in out if r.mode == "fit"]
    assert len(fit_pieces) == 1 and fit_pieces[0].points == (), "fit sub-range must stay fit"
    # no follower survives; every fill piece is the static pin
    assert all(not (r.mode == "fill" and len(r.points) > 1) for r in out)
    assert all(r.points[0].cx == 0.6 for r in out if r.mode == "fill")
    # boundaries frame-snapped, contiguous, t0/t1 span preserved (Δ=0)
    assert out[0].t0 == 0.0 and out[-1].t1 == region_end
    for prev, nxt in zip(out, out[1:], strict=False):
        assert prev.t1 == nxt.t0
    for r in out:
        assert _frame_error(r.t0, FPS_2997) < _FRAME_EPS
        assert _frame_error(r.t1, FPS_2997) < _FRAME_EPS


def test_follow_two_distinct_fill_overrides_keep_own_centers():
    # Genuine multi-override "force a sub-range" split must still split AND keep each distinct
    # center: two non-overlapping fill overrides (0.3 and 0.9) on one follow region. The overridden
    # pieces keep their own centers; the non-overridden follower slivers are absorbed (Follow never
    # survives on an overridden shot). Proves the fix does NOT collapse genuine sub-range edits.
    iv = SourceInterval(source_start=10.0, source_end=16.0)
    region_end = 180 / FPS_2997
    regions = [_follow_region(region_end)]
    ov = [
        CropOverride(source_start=11.0, source_end=12.0, mode="fill", center=0.3),  # rel 1..2
        CropOverride(source_start=14.0, source_end=15.0, mode="fill", center=0.9),  # rel 4..5
    ]
    out = apply_overrides_to_regions(regions, ov, iv, fps=FPS_2997)

    assert len(out) > 1, "two distinct sub-range overrides must still SPLIT the region"
    centers = {r.points[0].cx for r in out if r.mode == "fill"}
    assert 0.3 in centers and 0.9 in centers, "each distinct manual center must survive"
    # the specific overridden sub-ranges carry their own centers
    mid_lo = next(r for r in out if abs(r.t0 - round(1.0 * FPS_2997) / FPS_2997) < _FRAME_EPS)
    mid_hi = next(r for r in out if abs(r.t0 - round(4.0 * FPS_2997) / FPS_2997) < _FRAME_EPS)
    assert mid_lo.points[0].cx == 0.3 and mid_hi.points[0].cx == 0.9
    # no follower survives; boundaries frame-snapped + contiguous (Δ=0)
    assert all(not (r.mode == "fill" and len(r.points) > 1) for r in out)
    assert out[0].t0 == 0.0 and out[-1].t1 == region_end
    for prev, nxt in zip(out, out[1:], strict=False):
        assert prev.t1 == nxt.t0
        assert _frame_error(nxt.t0, FPS_2997) < _FRAME_EPS


def test_build_persist_payload_stores_fps():
    # P0: the persisted plan must carry the native fps its region boundaries were frame-snapped on,
    # so a later plan-read render can gate on fps-consistency (else -1 flash on ≠25fps, see
    # REFRAME_FPS_GRID_INVARIANT / parse_fps postmortem).
    from app.editor.reframe_cache import build_persist_payload

    regions = [
        TrackRegion(t0=0.0, t1=2.0, mode="fill", points=(TrackPoint(t=0.0, mode="fill", cx=0.4),))
    ]
    payload = build_persist_payload(regions, 10.0, 20.0, fps=29.97002997)
    assert payload["fps"] == 29.97002997
    assert payload["default_start"] == 10.0
    assert payload["default_end"] == 20.0
    assert payload["regions"][0]["mode"] == "fill"


def _sample_persisted(fps=30.0):
    return {
        "default_start": 10.0,
        "default_end": 20.0,
        "fps": fps,
        "regions": [
            {
                "t0": 0.0,
                "t1": 4.0,
                "mode": "fill",
                "points": [{"t": 0.0, "mode": "fill", "cx": 0.4}],
                "points_b": [],
                "fit_style": "blur_zoom",
            },
            {
                "t0": 4.0,
                "t1": 10.0,
                "mode": "fit",
                "points": [],
                "points_b": [],
                "fit_style": "blur_zoom",
            },
        ],
    }


def test_plan_to_regions_authoritative_untrimmed():
    from app.editor.reframe_cache import plan_to_regions

    regions = plan_to_regions(
        _sample_persisted(), [SourceInterval(source_start=10.0, source_end=20.0)], [], fps=30.0
    )
    assert regions is not None and len(regions) == 1
    assert [r.mode for r in regions[0]] == ["fill", "fit"]
    assert regions[0][0].points[0].cx == 0.4
    assert regions[0][0].t0 == 0.0 and regions[0][1].t1 == 10.0


def test_plan_to_regions_none_on_trim():
    from app.editor.reframe_cache import plan_to_regions

    assert (
        plan_to_regions(
            _sample_persisted(), [SourceInterval(source_start=12.0, source_end=20.0)], [], fps=30.0
        )
        is None
    )


def test_plan_to_regions_none_on_fps_mismatch():
    from app.editor.reframe_cache import plan_to_regions

    assert (
        plan_to_regions(
            _sample_persisted(fps=29.97),
            [SourceInterval(source_start=10.0, source_end=20.0)],
            [],
            fps=25.0,
        )
        is None
    )


def test_plan_to_regions_none_when_legacy_no_fps():
    from app.editor.reframe_cache import plan_to_regions

    p = _sample_persisted()
    del p["fps"]
    assert (
        plan_to_regions(p, [SourceInterval(source_start=10.0, source_end=20.0)], [], fps=30.0)
        is None
    )


def test_plan_to_regions_applies_override_repaint():
    from app.editor.reframe_cache import plan_to_regions

    ov = CropOverride(source_start=10.0, source_end=14.0, mode="fit")  # force first shot wide
    regions = plan_to_regions(
        _sample_persisted(), [SourceInterval(source_start=10.0, source_end=20.0)], [ov], fps=30.0
    )
    assert regions is not None
    assert regions[0][0].mode == "fit"  # repainted, boundaries preserved
    assert regions[0][0].t0 == 0.0 and regions[0][0].t1 == 4.0
