# P1 — Per-shot framing inspector (pan / mode / background, per shot) Implementation Plan

> **For agentic workers:** implement task-by-task; TDD the pure override-building logic. Steps use `- [ ]`.
> Parent spec: `docs/superpowers/specs/2026-07-11-editor-per-shot-reframe-timeline-downloads-design.md` §6.
> Prior phase: P0 (render reads plan) DONE @ `fd76374`. Branch `feat/editor-reframe-nle`. Local-first.

**Goal:** Fix the #1 complaint — "changing crop/background in one shot applies to the whole clip." Make the
Frame-panel controls (mode, crop-pan, wide-background) act on the **selected shot** in `FitTimeline`, plus
**drag-to-pan directly in the preview** for the selected shot. Each edited shot is visibly "manual"
(provenance derived from override presence).

**Architecture:** The backend already supports per-shot framing — `CropOverride{source_start, source_end,
mode, center, center_b, fit_style}` is range-scoped (`models.py:197`), `apply_overrides_to_regions`
interval-paints it grid-safely (`reframe_cache.py:87`), and `op_crop`/`setCropOverride` accept an arbitrary
sub-range (`main.py:1913`). **So P1 is FRONTEND wiring** — the per-shot path just stops sending `center:null`
and starts carrying `center` + `fit_style`; the preview gains a horizontal drag that writes the selected
shot's `center`.

**Tech Stack:** Next 16 / React (read `apps/web/AGENTS.md` FIRST — Next 16 caveat), TypeScript, existing
editor components. Types are codegen (`packages/shared`) — **do not hand-edit**; `CropOverride` already has
`center`/`fit_style`, so **no `models.py`/`just types` change is expected** in P1.

## Global Constraints

- **No whole-clip leakage:** a control edit while a shot is selected must write ONLY that shot's
  `[source_start, source_end]` range — never the clip's outer bounds.
- **Grid invariant untouched:** P1 does not change reframe math or render; it only sets `center`/`fit_style`
  on a range the backend already frame-snaps. Do NOT touch `stage3/stage5/reframe_cache` render logic.
- **i18n:** match the existing pattern in the editor files you touch (if they use `t()`, add keys to BOTH
  `apps/web/messages/{en,ru}.json`; if they're literal English, keep new labels English — the editor i18n
  state is mixed, follow the file you're in). Never hardcode a key that exists in only one catalog.
- **Gate:** `just check` green before commit (orchestrator runs it). Stop `next dev` before `just check`
  (Turbopack `.next/dev/types` breaks tsc). Commit from PowerShell.
- **No silent fallbacks.** Preview crop must reflect the written override immediately (WYSIWYG).

---

## File map (frontend only)

- `apps/web/components/editor/FitTimeline.tsx` — per-shot strip. Add a clear **selected-shot** state
  (single-shot select, not just range-drag) + a "manual" marker on shots that have an override.
- `apps/web/components/editor/FrameTab.tsx` — the controls (mode auto/fill/fit, crop-center slider,
  fit-style blur_zoom/blur/black). When a shot is selected, these bind to the **selected shot**; the header
  says which shot is being edited. Keep a "whole clip" affordance only as an explicit choice.
- `apps/web/components/editor/ClipEditorScreen.tsx` — `handleApplyRange` (~:1316) must carry `center` +
  `fit_style` (not `center:null`). Add selected-shot state + a `setShotPan(shotRange, cx)` path. Reconcile
  with `handleFrameApply` (~:1263) so there is ONE override-builder (DRY).
- `apps/web/components/editor/PreviewPlayer.tsx` — add horizontal **drag-to-pan**: while a fill shot is
  selected, dragging the video updates that shot's `center` (cx), applied imperatively via the existing
  `applyCrop` in rVFC; commit the override on pointer-up.
- `apps/web/lib/reframeFrame.ts` (or the pure override helper) — the pure logic to build/replace a per-shot
  override and to derive `origin` (a shot is "manual" iff an override covers it). **TDD this** (`*.test.ts`).

---

### Task 1: Pure per-shot override builder + provenance (TDD)

**Files:** create/extend `apps/web/lib/reframeFrame.ts` + `apps/web/lib/reframeFrame.test.ts`.

**Interfaces:**
- Produces: `upsertShotOverride(overrides: CropOverride[], shot: {source_start,source_end}, patch: {mode?, center?, fit_style?}): CropOverride[]` — replaces any override on that exact shot range, else appends; returns a new array. `shotIsManual(overrides, shot): boolean` — true iff an override covers the shot's midpoint.

- [ ] **Step 1: Write failing tests** — cover: (a) upsert appends when none exists; (b) upsert replaces the existing override on the same range (no duplicates); (c) editing shot B leaves shot A's override untouched; (d) `shotIsManual` true only for covered shots. Write concrete `expect(...)` on the returned array.
- [ ] **Step 2: Run — verify fail** (`cd apps/web; npx vitest run lib/reframeFrame.test.ts`).
- [ ] **Step 3: Implement** the two pure functions (immutable array ops; match on `source_start/source_end` within a small epsilon).
- [ ] **Step 4: Run — verify pass.**
- [ ] **Step 5:** (commit folded into the phase commit by the orchestrator.)

### Task 2: Frame controls bind to the selected shot

**Files:** `FitTimeline.tsx`, `FrameTab.tsx`, `ClipEditorScreen.tsx`.

- [ ] Add single-shot selection to `FitTimeline` (click a shot → selected; highlight it). Selecting a shot sets `selectedShot={source_start, source_end}` in `ClipEditorScreen`.
- [ ] `FrameTab` header shows "Editing shot N (mm:ss–mm:ss)" when a shot is selected, else "Whole clip".
- [ ] Wire mode tiles + crop-center slider + fit-style so that, when a shot is selected, they call a single handler that does `upsertShotOverride(selectedShot, patch)` → `setCropOverride` on the shot's range (carrying `center` + `fit_style`, NOT null). Reuse ONE override-builder for both per-shot and whole-clip (DRY; remove the `center:null` in `handleApplyRange`).
- [ ] Mark shots with an override as "manual" in `FitTimeline` (small dot/label via `shotIsManual`); untouched shots read as AI.
- [ ] **Acceptance:** select shot 3, set Tight + move the crop slider + set background=dark → only shot 3 changes; shot 4 keeps its AI framing; the preview updates instantly at shot 3's time.

### Task 3: Drag-to-pan in the preview

**Files:** `PreviewPlayer.tsx`, `ClipEditorScreen.tsx`.

- [ ] While a **fill** shot is selected, a horizontal pointer-drag on the preview updates that shot's `center` live (imperative `applyCrop` inside the existing rVFC path — do NOT route through React state during the gesture, per the preview-lag lesson). On pointer-up, commit via `upsertShotOverride` → `setCropOverride`.
- [ ] Clamp `center` to the valid crop range so the 9:16 window stays inside the source width.
- [ ] Show a subtle "drag to pan" affordance only when a fill shot is selected.
- [ ] **Acceptance:** drag left/right on shot 3 → the crop pans within shot 3 only; release → persists; re-open the clip → shot 3 keeps the panned center; other shots unaffected.

---

## Out of scope for P1 (later phases)

- Per-shot **follow on/off** toggle (needs the richer Frame Plan follow field; keep the whole-clip
  Lock/Follow for now — P4/model work). Add/move/delete cut = **P3**. CapCut timeline = **P2**.

## Self-Review checklist (orchestrator, before commit)

- Spec §6 coverage: per-shot mode ✅ (existed) + per-shot pan (T2/T3) + per-shot background (T2) + provenance
  (T1) + drag-to-pan (T3). Per-shot follow explicitly deferred (noted).
- No whole-clip write path remains reachable from a shot-selected control.
- `just check` green; preview updates without a server render (WYSIWYG); no `models.py`/types change.

## Execution

Dispatch one frontend subagent to implement T1–T3 (reads current code + `apps/web/AGENTS.md`, TDD on
`reframeFrame.ts`, runs `npx tsc`/`vitest`/`eslint`, does NOT commit). Orchestrator reviews the diff, runs
`just check`, and commits the plan + implementation together. Verify T2/T3 acceptance in a real browser
before calling P1 done (the WYSIWYG per-shot behavior is a visual claim).
