# P3 — Manual cut editing (split / move / merge a shot boundary), persisted Implementation Plan

> Parent spec §6b: `docs/superpowers/specs/2026-07-11-editor-per-shot-reframe-timeline-downloads-design.md`.
> Prior: P0 `fd76374` (render reads plan), P1 `8dd2d0c`, P1-bugfix `dc9af74`, P2 `0d058c6`,
> crop-fix `d6475f7`. Branch `feat/editor-reframe-nle`. Local-first. **Touches the sacred Δ=0 grid →
> read `docs/REFRAME_FPS_GRID_INVARIANT.md` first + frame-step verification is MANDATORY.**

**Goal:** Let the monteur fix the shot segmentation when the AI got it wrong — **add** a cut the AI missed,
**merge** an AI cut that shouldn't be there, and **move** a boundary — persisted, and reflected identically in
preview AND render.

**Why it's mostly grid-safe already (recon):** `flatten_timeline` (`stage5_render.py:687-720`) anchors source
frames on `aligned = round(source_start*fps)/fps` and lays ANY region boundary exactly
(`src_f0 = round((aligned+t0)*fps)`); `apply_overrides_to_regions` already re-cuts regions at frame-snapped
boundaries. So a manual cut = an extra frame-snapped region boundary the render already renders correctly; a
merge = dropping a boundary between two adjacent regions. The ONLY missing piece today: overrides can only ADD
boundaries (a sub-range override), never REMOVE an AI cut, and `userSplits` (client-only, `FitTimeline`) never
persists. P3 adds the minimal persisted data + a pure region transform.

**Architecture:** Extend `ClipEdit` with two frame-snapped source-second lists, then apply them as a PURE
transform right after `apply_overrides_to_regions` on BOTH the render path and the preview path (so
preview == render). No new render code — reuse the existing frame-snapped grid.

## Global constraints
- **Δ=0 grid:** every introduced/kept boundary passes through `_snap(t,fps)=round(t*fps)/fps`; region t0/t1
  stay on-grid. Frame-step a real ≠25fps render at every new/merged seam (unit tests do NOT catch a
  content-splice flash).
- **Type contract:** `ClipEdit` change = `models.py` → **`just types`** (never hand-edit `packages/shared`).
- **No silent fallbacks.** A manual cut outside the clip / a suppressed time matching no AI boundary → ignored
  with a log, not a crash.
- **Commit gate** `just check`; commit from PowerShell. Stop `next dev` before tsc.

## Data model (models.py — `ClipEdit`)
```
manual_cuts:     list[float] = []   # SOURCE seconds; add a boundary here (frame-snapped at apply time)
suppressed_cuts: list[float] = []   # SOURCE seconds near an AI/scene boundary → merge across it
```
Both are source-absolute (survive trim, like `reframe_overrides`). Frozen-empty default = existing clips
unaffected.

## Tasks

### Task 1: Pure `apply_cuts_to_regions` (TDD) — `reframe_cache.py`
`apply_cuts_to_regions(regions, manual_cuts, suppressed_cuts, iv, fps) -> list[TrackRegion]`:
- **Split:** for each `manual_cut` in `[iv.source_start, iv.source_end]`, snap to interval-relative
  `_snap(cut - iv.source_start, fps)`; if strictly interior to some region, split that region into two pieces
  at the snapped time — BOTH pieces inherit the region's mode; a `fill` region's single/– points are carried
  (the leading piece keeps `points`, the trailing piece re-anchors a single `TrackPoint(t=split, cx)` so a
  follower is not duplicated). Dedup against existing boundaries (no zero-length sliver).
- **Merge:** for each `suppressed_cut`, find the region boundary within a small tolerance and drop it —
  concatenate the two adjacent regions into one spanning `[prev.t0, next.t1]`, keeping the FIRST region's
  mode+points (design decision below). Never merge across a `fit`↔`fill` change unless the user explicitly
  did (keep it simple: merge always keeps the first region's framing; the user then re-picks mode/pan on the
  merged shot via P1).
- Pure; every boundary frame-snapped; t0/t1 on-grid. Tests: split interior (2 pieces, snapped, contiguous);
  split on existing boundary (no-op / no sliver); merge drops a boundary (N→N-1, span preserved); suppressed
  time matching nothing (ignored); manual cut outside clip (ignored); ≠25fps frame-snap asserted.

### Task 2: Wire into render + preview — `reframe_cache.py`, `tasks.py`, `main.py`
- `plan_to_regions` (P0) and `resolve_regions_accurate` (CV path) and `regions_from_persisted` (preview): after
  `apply_overrides_to_regions`, call `apply_cuts_to_regions(...)` with `edit.manual_cuts`/`edit.suppressed_cuts`.
  One shared helper; identical on all three so preview == render. (Thread the two lists through the same call
  sites that already pass `overrides`.)
- `get_clip_reframe` fast-path + render both apply cuts. Confirm the persisted-plan `fps` gate still holds.

### Task 3: Endpoints — `main.py`
- `POST /jobs/{id}/clips/{clip}/edit/cut {source_time}` → append a frame-snapped time to `manual_cuts` (or,
  if within tolerance of an existing AI boundary, add to `suppressed_cuts` = a merge). Version-bump like
  `op_crop`. `DELETE`/`{action:"clear"}` to remove a manual cut / un-suppress. No silent fallback.

### Task 4: Editor UI — `FitTimeline.tsx`, `ClipEditorScreen.tsx`
- "Split here" (already exists, client-only `userSplits`) now **persists** via `POST …/edit/cut` → the split
  survives reload + reaches the render. A boundary between two shots gets a **merge (✕)** affordance →
  `suppressed_cuts`. **Move** = suppress the old + add the new (two calls). Reuse the P1-bugfix rule: a cut
  edit recolors locally, NO `/reframe` refetch reshuffle (the plan is now authoritative).
- i18n keys in BOTH `messages/{en,ru}.json`.

### Task 5: Δ=0 verification (MANDATORY) — scratch harness
- Extend `tmp/verify_p0_grid.py`: on a real ≠25fps clip, add a manual cut mid-shot + a merge of an AI cut →
  render → frame-step both new seams. A split seam must be a clean hard cut on the exact native frame; a merged
  seam must show ONE continuous framing (no residual flash). Eyeball + assert region t0/t1 on-grid.

## Design decision to confirm with founder
**Merge framing:** when you merge two AI shots into one, whose framing wins? Plan: **the first shot's** framing
carries across the merged shot, and the user re-picks mode/pan on it via the P1 inspector if needed. (Simpler +
predictable vs. trying to auto-reconcile two different crops.) Confirm or override.

## Execution
Full-stack + grid-sensitive → I (orchestrator, Opus) drive Tasks 1-3 (worker, TDD + frame-step) directly, and
a frontend subagent does Task 4 (FitTimeline/ClipEditorScreen wiring) with strict file boundaries. Task 5
(real-render Δ=0) I run directly. `just types` after the models.py change. Gate + commit by orchestrator.
