# P2 — CapCut-grade editor timeline (instant seek + clear trim + snap) Implementation Plan

> Parent spec §7: `docs/superpowers/specs/2026-07-11-editor-per-shot-reframe-timeline-downloads-design.md`.
> Prior: P0 `fd76374`, P1 `8dd2d0c`, P1-bugfix `dc9af74`. Branch `feat/editor-reframe-nle`. Local-first.

**Goal:** Make the editor's bottom timeline behave like CapCut. Fixes two founder complaints:
1. **"Clicking the timeline doesn't seek immediately."** Root cause (recon): `TimelineV2` has NO click-to-seek;
   a click either does nothing (empty track) or hits a marker → `handleSetInterval` = a version-bumping
   SERVER round-trip (`ClipEditorScreen.tsx:906`) that re-trims + refetches → feels laggy. Only dragging the
   clip window seeks.
2. **"Setting where the clip starts / its length / where it ends is confusing, worse than CapCut."** The
   draggable clip window exists (`TimelineV2.tsx:293-308`, move + L/R grips, clamp `CLIP_MIN_SEC`/`MAX`) but:
   slow server commit on release, **no numeric fields**, **no snapping** to the hover line / markers / shot
   boundaries, not discoverable.

**Architecture:** Rework `TimelineV2.tsx` in place, borrowing the **proven NLE scrubber** already built on the
`feat/video-map-cockpit` branch — read it first:
`git show feat/video-map-cockpit:apps/web/components/videomap/MapTimeline.tsx`
(its pattern: **click = instant seek** off a local `scrubT`, **drag = scrub** with the video seek coalesced
1×/rAF (lag-free), a **Scrub/Select toggle**, Ctrl+wheel zoom, hover tooltip). Bring that seek/scrub model
into `TimelineV2` while KEEPING its existing clip-window trim (grips) + waveform + chapters/moment ticks.

**Tech Stack:** Next 16 / React (read `apps/web/AGENTS.md` first), TypeScript, existing editor components.

## Global Constraints

- **Instant seek is CLIENT-ONLY.** A click/scrub sets `video.currentTime` directly (via the existing preview
  seek path) — it must NOT call `handleSetInterval` or any worker endpoint. Seeking ≠ trimming.
- **Trimming stays server-committed on grip release** (`handleSetInterval` → `setClipInterval`), UNCHANGED in
  contract. Do NOT change how the interval persists. (Interval change legitimately still calls `loadReframe`
  — that's correct; the P1-bugfix only removed `loadReframe` from the per-shot FRAMING path, not trim.)
- **Grid/reframe untouched** — P2 is timeline UI only. Do NOT touch `stage3/stage5/reframe_cache` or the
  framing override logic.
- **i18n:** match the file's existing pattern; any new user-facing string → BOTH `messages/{en,ru}.json`.
- **Gate:** orchestrator runs `just check` + commits. You run `vitest` + `eslint` only — **do NOT run `tsc`**
  (a `next dev` server is running; its `.next/dev/types` breaks tsc — the orchestrator handles tsc at gate
  time). Do NOT commit.

## File map (frontend only)

- `apps/web/components/editor/TimelineV2.tsx` — the rework: click-to-seek, drag-scrub (rAF-coalesced),
  Scrub/Select toggle, snap-on-trim, numeric readout→inputs. (~690 lines today; keep it focused — if it grows
  unwieldy, extract a small pure `lib/timelineSnap.ts` helper.)
- `apps/web/components/editor/ClipEditorScreen.tsx` — add a **client-only seek** handler wired to the preview
  (`video.currentTime` via the existing PreviewPlayer seek ref/`handleScrub`), passed to `TimelineV2` as an
  `onSeek` prop. Keep `handleSetInterval` for trim commits only. Add numeric start/end/duration commit.
- `apps/web/lib/timelineSnap.ts` (+ `.test.ts`) — **TDD** the pure snap logic: given a candidate time and a
  set of snap targets (hover line, moment ticks, shot boundaries, chapter edges) + a pixel tolerance, return
  the snapped time (or the raw time if none within tolerance). Pure, unit-tested.

## Tasks

### Task 1: Pure snap helper (TDD)
- `snapTime(t, targets: number[], tolSec): number` — returns the nearest target within `tolSec`, else `t`.
  Tests: snaps when within tol; returns raw when outside tol; picks the NEAREST of several; empty targets → t.

### Task 2: Click-to-seek + drag-scrub (the lag fix)
- Add `onSeek(t)` to `TimelineV2` props; `ClipEditorScreen` passes a client-only seek (sets the preview
  `video.currentTime` via the existing rAF-coalesced `handleScrub`/seek ref — NO server call).
- `TimelineV2`: a plain **click** on the track seeks to that time instantly (playhead jumps immediately).
  **Drag** in Scrub mode scrubs (playhead follows the cursor off a local `scrubT`, seek coalesced 1×/rAF).
  Add a **Scrub/Select** toggle so a click seeks (Scrub) vs. selects a marker (Select) — mirror `MapTimeline`.
- Marker/chapter click in Scrub mode = **seek** (not `handleSetInterval`). The old "click a moment → re-trim"
  behavior moves to an explicit control (e.g. only when dragging the window onto it), so a click never
  triggers a server round-trip.
- **Acceptance:** clicking anywhere on the timeline moves the playhead/preview INSTANTLY, with no network
  request and no "saving" state.

### Task 3: Clear trim (numeric + snap)
- Keep the draggable clip window (move + L/R grips). While dragging a grip or the body, **snap** the in/out
  edge to the nearest of: hover line, moment ticks, shot boundaries, chapter edges (via `snapTime`, Task 1),
  showing the snap visually.
- Add editable **numeric start / end / duration** fields (the current read-only `mmss` readout becomes
  inputs); committing a field calls the SAME `handleSetInterval` path (clamped to `CLIP_MIN_SEC`/`MAX`).
- **Acceptance:** the user can set the clip start, end, and length by dragging (with snap) OR typing exact
  values; the window and preview reflect it; commit persists via the existing interval endpoint.

## Reference & verification
- Read `git show feat/video-map-cockpit:apps/web/components/videomap/MapTimeline.tsx` for the proven
  seek/scrub/zoom/toggle implementation — adapt, don't copy blindly (TimelineV2 has waveform + clip window +
  trim that MapTimeline lacks).
- Before returning: run `cd apps/web; npx vitest run` and `npx eslint components/editor/TimelineV2.tsx
  components/editor/ClipEditorScreen.tsx lib/timelineSnap.ts`. Report PASS/FAIL. Do NOT run tsc (dev running).
- Return a concise per-file summary + the commands+results + what the orchestrator must verify in a real
  browser (click-to-seek instant, snap-on-trim, numeric fields).
