# Video Map Cockpit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the Video Map from a passive "why these clips are worth it" essay into an interactive
cockpit (source preview + whole-video timeline with coverage + clickable outline) that also lets Pro/Studio
editors **extract new clips on-demand** from un-clipped moments.

**Architecture:** Frontend-first. M1 rebuilds `VideoMap.tsx` as a cockpit fed by data already on the wire
(+ a fixture dev-harness for no-reprocess preview). M2 adds a pure, TDD'd boundary helper. M3 adds the
`POST /jobs/{id}/clips` endpoint that reuses the existing per-clip reframe+render fan-out. M4 adds server-side
economics/gating. M5 polishes.

**Tech Stack:** Next.js (⚠️ MODIFIED — see Global Constraints), React 19, TypeScript, Tailwind (design tokens),
Python 3 worker (FastAPI), Gemini, Cloud Run render fan-out, Supabase, pytest, ruff/mypy/tsc/eslint via `just`.

## Global Constraints

- **Next.js is MODIFIED** (`apps/web/AGENTS.md`): read `node_modules/next/dist/docs/` before writing web code.
- **Types are codegen** from `services/worker/app/models.py` → `just types`. NEVER hand-edit `packages/shared/*`.
- **Money paths:** `services/worker/app/billing.py` is source of truth; mirror in `apps/web/lib/plans.ts` (change BOTH).
- **No silent fallbacks:** errors surface as `JobError` / failed status / explicit UI state.
- **Reframe invariant:** do NOT touch `stage3_reframe`/`stage5_render`/`reframe_cache`; reuse the existing
  `reframe_render_clip` fan-out unchanged.
- **i18n:** every new user-facing string → key in BOTH `apps/web/messages/{en,ru}.json` via `t()`; RU idiomatic.
- **Commit gate:** `just check` green before every commit; commit from PowerShell (pre-commit hook needs `just`
  on refreshed PATH — takes >2 min, allow ≥8 min timeout); conventional commits; Cyrillic messages via `-F file`.
- **Local-test only; DO NOT deploy.** Branch `feat/video-map-cockpit`.
- **Test-DB gotcha:** worker unit tests share a persistent tmp `jobs.db` → use fresh uid + unique job_ids
  in usage/quota tests (`worker-test-db-persist-gotcha`).
- **Before `just check`:** stop any `next dev` (Turbopack `.next/dev/types` breaks tsc).

---

## File Structure

**Frontend (create):**
- `apps/web/components/videomap/VideoMapCockpit.tsx` — orchestrator (fetch/poll + shared playhead state).
- `apps/web/components/videomap/SourcePreview.tsx` — inline `preview.mp4` player, imperative rVFC seek/playhead.
- `apps/web/components/videomap/MapTimeline.tsx` — horizontal timeline: chapter segments + moment ticks + coverage band + drag-select.
- `apps/web/components/videomap/MapOutline.tsx` — chapter/moment outline list (click→seek), reuses accordion.
- `apps/web/components/videomap/MomentRow.tsx` — one moment: ● clip chip / ○ untapped + extract action.
- `apps/web/components/videomap/ExtractButton.tsx` — extract CTA with gate/paywall/expiry states.
- `apps/web/components/videomap/NarrativeCollapse.tsx` — demoted narrative (reuses `parseNarrative`).
- `apps/web/components/videomap/coverage.ts` — pure helpers: `classifyMoments`, `coverageSegments`, `momentClipId`.
- `apps/web/app/(app)/dev/video-map/page.tsx` — fixture harness (no reprocess).
- `apps/web/lib/fixtures/videoMapCardiB.ts` — saved Cardi B fixture (video_map + clips + preview url).

**Frontend (modify):**
- `apps/web/components/VideoMap.tsx` — becomes a thin re-export/shim of `VideoMapCockpit` (keep mount stable).
- `apps/web/lib/api.ts` — add `createClipFromRange`.
- `apps/web/lib/plans.ts` — mirror extraction gate/econ.
- `apps/web/messages/{en,ru}.json` — new keys.

**Worker (create):**
- `services/worker/app/editor/extract_clip.py` — pure `range_to_segment(...)` + orchestration `build_extracted_clip(...)`.
- `services/worker/tests/test_extract_clip.py` — TDD for boundary correctness + idempotency.
- `services/worker/tests/test_extract_billing.py` — TDD for gate + ≤30 free / >30 paywall.

**Worker (modify):**
- `services/worker/app/main.py` — `POST /jobs/{id}/clips`.
- `services/worker/app/billing.py` — `can_extract_clip(plan)`, `extract_clip_charge(job, plan)`.
- `services/worker/app/db.py` / `cloud_state.py` — `append_pending_clip(...)` (atomic).
- `services/worker/app/tasks.py` — spawn `reframe_render_clip` for the appended clip.
- `services/worker/app/models.py` — `ClipOut.origin` ("auto"|"extracted"); `just types` after.

---

## Milestone 1 — Cockpit navigation (frontend-only, viewable, no backend)

> Delivers a viewable cockpit on localhost via the fixture harness (founder's no-reprocess ask). Uses only
> data already on the wire. Extraction button renders but is disabled/"coming" until M3.

### Task 1.1: Cardi B fixture + dev harness route

**Files:**
- Create: `apps/web/lib/fixtures/videoMapCardiB.ts`
- Create: `apps/web/app/(app)/dev/video-map/page.tsx`

**Interfaces:**
- Produces: `export const CARDI_B_FIXTURE: { jobId: string; map: VideoMap; clips: ClipOut[]; previewUrl: string }`

- [ ] **Step 1:** Capture a real fixture. Run the worker locally once on the Cardi B source (or pull the
  persisted `video_map.json` + clips from a prior local job under the worker tmp dir) and hand-copy into
  `videoMapCardiB.ts` as typed literals (`VideoMap` + `ClipOut[]`). `previewUrl` = a local/CDN `preview.mp4`
  (`https://cdn.quip.ink/<job_id>/preview.mp4` if a real job exists, else a bundled sample in `public/`).
  Shape reference: `apps/web/app/api/mock/jobs/[id]/video-map/route.ts`.
- [ ] **Step 2:** Create `dev/video-map/page.tsx` that renders `<VideoMapCockpit jobId={fx.jobId} clips={fx.clips}
  initialMap={fx.map} previewUrl={fx.previewUrl} />` (client wrapper). Gate the route to dev only
  (`if (process.env.NODE_ENV === "production") notFound()`).
- [ ] **Step 3:** `npm run build` (or `just check` web slice) — expect typecheck pass. Visually open
  `http://localhost:3000/dev/video-map` after `next dev` — expect fixture data to render (initially via the
  current VideoMap until 1.2+ land).
- [ ] **Step 4:** Commit `feat(video-map): Cardi B fixture + /dev/video-map harness (no-reprocess preview)`.

### Task 1.2: Coverage pure helpers (+ fix the ○-moment lie)

**Files:**
- Create: `apps/web/components/videomap/coverage.ts`
- Create: `apps/web/components/videomap/__tests__/coverage.test.ts` (vitest/jest per repo config)

**Interfaces:**
- Produces:
  - `momentClipId(clips: ClipOut[], m: {start:number;end:number}): string | null` — clip whose [start,end]
    OVERLAPS the moment (not just contains a point); null if none. **Replaces the buggy point-containment +
    `chapterClipIds[0]` fallback.**
  - `classifyMoment(clips, m): "clipped" | "untapped"`
  - `coverageSegments(durationSec: number, clips: ClipOut[]): {start:number;end:number;clipped:boolean}[]` —
    merged intervals over [0,duration] flagged clipped/untapped.

- [ ] **Step 1: Write failing tests.**

```ts
import { momentClipId, classifyMoment, coverageSegments } from "../coverage";
const clips = [{ id: "clip_01", start: 100, end: 130 }, { id: "clip_02", start: 300, end: 340 }] as any;
test("overlap picks the clip", () => { expect(momentClipId(clips, { start: 120, end: 145 })).toBe("clip_01"); });
test("no overlap → null (no fallback lie)", () => { expect(momentClipId(clips, { start: 200, end: 220 })).toBeNull(); });
test("classify", () => { expect(classifyMoment(clips, { start: 200, end: 220 })).toBe("untapped"); });
test("coverage merges + flags gaps", () => {
  const segs = coverageSegments(400, clips);
  expect(segs.some((s) => s.clipped && s.start === 100)).toBe(true);
  expect(segs.some((s) => !s.clipped && s.start === 130)).toBe(true);
});
```

- [ ] **Step 2:** Run tests → expect FAIL (module missing).
- [ ] **Step 3:** Implement `coverage.ts` (interval overlap = `a.start < b.end && b.start < a.end`; merge sorted clip intervals; walk [0,duration] emitting clipped/untapped segments).
- [ ] **Step 4:** Run tests → expect PASS.
- [ ] **Step 5:** Commit `feat(video-map): coverage helpers + fix ○-moment mislink`.

### Task 1.3: SourcePreview (inline player, imperative rVFC playhead)

**Files:** Create `apps/web/components/videomap/SourcePreview.tsx`

**Interfaces:**
- Consumes: `previewUrl: string`, `onTime(sec:number):void`, `seekRef: React.RefObject<(sec:number)=>void>`.
- Produces: a `<video>` that (a) exposes an imperative `seek(sec)` via `seekRef` (sets `currentTime`, plays),
  (b) drives `onTime` per-frame via `requestVideoFrameCallback` (NOT `timeupdate`), (c) falls back to
  `timeupdate` only if rVFC unsupported.

- [ ] **Step 1:** Build the component: `<video src={previewUrl} playsInline>`; on mount register a rVFC loop
  updating `onTime(v.currentTime)`; assign `seekRef.current = (s)=>{ v.currentTime = s; v.play(); }`. Clean up rVFC on unmount.
- [ ] **Step 2:** `tsc` + `eslint` clean. Visual: on `/dev/video-map`, calling seek from a temp button moves the frame.
- [ ] **Step 3:** Commit `feat(video-map): inline source preview with per-frame playhead`.

### Task 1.4: MapTimeline (chapters + moment ticks + coverage + drag-select)

**Files:** Create `apps/web/components/videomap/MapTimeline.tsx`

**Interfaces:**
- Consumes: `chapters`, `clips`, `durationSec`, `playheadSec`, `onSeek(sec)`, `onSelectRange({start,end})`.
- Produces: a horizontal SVG/flex timeline; x = time/duration. Layers: chapter segments (labeled, click→seek),
  moment ticks (colored by `kindColor`, click→seek), coverage band (from `coverageSegments`), a playhead line,
  and pointer drag → `onSelectRange`. Virtualize ticks only if count is large (YAGNI until needed).

- [ ] **Step 1:** Render chapters as proportional segments + coverage band beneath + moment ticks; bind clicks to `onSeek`.
- [ ] **Step 2:** Add playhead line positioned at `playheadSec/durationSec`.
- [ ] **Step 3:** Add pointer drag (pointerdown→move→up) computing a `{start,end}` range → `onSelectRange`; show a selection overlay.
- [ ] **Step 4:** `tsc`+`eslint` clean; visual on `/dev/video-map`: clicking a chapter/tick seeks; dragging selects.
- [ ] **Step 5:** Commit `feat(video-map): whole-video timeline with coverage + drag-select`.

### Task 1.5: MapOutline + MomentRow + ExtractButton(disabled) + NarrativeCollapse

**Files:** Create `MapOutline.tsx`, `MomentRow.tsx`, `ExtractButton.tsx`, `NarrativeCollapse.tsx`

**Interfaces:**
- `MomentRow`: uses `classifyMoment`; clipped → "Open clip #N" (`momentClipId`), untapped → `<ExtractButton>`;
  clicking the row body → `onSeek(moment.start)`.
- `ExtractButton` (M1): renders disabled with tooltip "Coming"; real behavior wired in M3/M4.
- `NarrativeCollapse`: `<details>` "Why these clips work" wrapping existing `parseNarrative` output.

- [ ] **Step 1:** Build the four components (reuse `kindColor`, `mmss`, `Numeral`, `Eyebrow`, design tokens).
- [ ] **Step 2:** `tsc`+`eslint` clean.
- [ ] **Step 3:** Commit `feat(video-map): outline + moment rows + demoted narrative`.

### Task 1.6: VideoMapCockpit orchestrator + shim old VideoMap

**Files:** Create `VideoMapCockpit.tsx`; Modify `apps/web/components/VideoMap.tsx` (shim), `dev/video-map/page.tsx`.

**Interfaces:**
- `VideoMapCockpit({ jobId, clips?, initialMap?, previewUrl? })`: keeps the existing poll (`getVideoMap`), or uses
  `initialMap` when provided (fixture path). Owns `playheadSec` + a `seekRef`; lays out SourcePreview + MapOutline
  (top) and MapTimeline (bottom) + NarrativeCollapse. `previewUrl` default = `${WORKER}/jobs/${jobId}/preview.mp4`
  (with media ticket per existing pattern — confirm against `api.ts` media-token flow).
- `VideoMap.tsx` re-exports `VideoMapCockpit` so `dashboard/page.tsx:433` mount is unchanged.

- [ ] **Step 1:** Compose the cockpit; wire seek (outline/timeline → `seekRef.current`) and `onTime` → `playheadSec`.
- [ ] **Step 2:** Replace `VideoMap.tsx` body with the shim; point the harness at `VideoMapCockpit`.
- [ ] **Step 3:** `just check` green (stop `next dev` first). Visual: `/dev/video-map` shows the full cockpit; the
  real results page still renders (via poll).
- [ ] **Step 4:** Commit `feat(video-map): assemble cockpit; keep results-page mount stable`.

---

## Milestone 2 — Boundary helper (pure, TDD, no endpoint)

### Task 2.1: `range_to_segment` — clean cuts identical to auto-clips

**Files:** Create `services/worker/app/editor/extract_clip.py`, `services/worker/tests/test_extract_clip.py`

**Interfaces:**
- Consumes (reuse, read signatures first): `_nearest_word_index` + `moment_to_interval` (`video_map.py:189,204`),
  `snap_start_index`/`snap_end_index`/`refine_boundaries_audio` (`stage2_select.py:260,178,301`).
- Produces: `range_to_segment(start_sec, end_sec, words: list[Word], *, loudness: list[float]|None, source_dur:
  float, min_sec: float = 20.0) -> Segment` — maps seconds→word idx, snaps start/end, audio-refines, applies −150ms
  safe pad + ≥20s, returns a `Segment` with clean `start/end`. Also `segment_key(seg) -> str` for idempotency.

- [ ] **Step 1: Write failing tests** (synthetic `Word` list + a loudness curve): clean start snaps to a
  sentence boundary; end snaps into a low-energy pause; filler words at edges are trimmed; result ≥ `min_sec`;
  start pad never crosses the previous word's end; `segment_key` stable for rounded bounds. (Fresh data per test.)
- [ ] **Step 2:** Run → FAIL (module missing).
- [ ] **Step 3:** Implement by composing the existing pure functions (read them first; do not reinvent snapping).
- [ ] **Step 4:** Run → PASS. Then `just check`.
- [ ] **Step 5:** Commit `feat(worker): range_to_segment — clean on-demand clip boundaries (TDD)`.

---

## Milestone 3 — Extraction endpoint + persist + render (local E2E)

### Task 3.1: `ClipOut.origin` + `append_pending_clip`

**Files:** Modify `models.py` (add `origin: Literal["auto","extracted"] = "auto"`), then `just types`; add
`append_pending_clip(job_id, clip: ClipOut)` to `db.py`/`cloud_state.py` (atomic append, mirror the RPC-0010
`jsonb` pattern used by `set_clip_video_url`).

- [ ] **Step 1:** Add field; `just types`; verify `packages/shared` regenerated (not hand-edited).
- [ ] **Step 2:** Implement + test atomic append (fresh uid + unique job_id). `just check`.
- [ ] **Step 3:** Commit `feat(worker): extracted-clip origin + atomic append_pending_clip`.

### Task 3.2: `POST /jobs/{id}/clips` → build + spawn render

**Files:** Modify `main.py` (endpoint behind `_require_owned_job`), `extract_clip.py`
(`build_extracted_clip(job, start, end)`), `tasks.py` (spawn `reframe_render_clip` for the new clip).

**Interfaces:**
- Endpoint body `{start:float, end:float}` → load words+loudness from artifacts → `range_to_segment` →
  seed hook/why/key_quote from the nearest moment `label`/`why` → `append_pending_clip` → spawn render → return
  the pending `ClipOut`. Idempotent by `segment_key`. 404/expired if source missing (60-day window; check via
  `storage` head) with an explicit message.

- [ ] **Step 1:** Wire endpoint (gate/econ added in M4; here just Pro-plan stub or open behind auth).
- [ ] **Step 2:** Local E2E on the real Cardi B source (uvicorn worker + inline render; PATH refresh; DELETE stale
  `clip_edits`): extract a `○` moment → clip streams into the grid → **inspect the cut frame-accurately by eye**.
- [ ] **Step 3:** `just check`; commit `feat(worker): POST /jobs/{id}/clips — on-demand extraction + render`.

### Task 3.3: Frontend wire — `createClipFromRange` + live ExtractButton

**Files:** Modify `apps/web/lib/api.ts`, `ExtractButton.tsx`, `MapTimeline` selection → extract CTA.

- [ ] **Step 1:** `createClipFromRange(jobId,{start,end})` → POST; optimistic pending clip + poll until ready
  (reuse existing streaming/`Rendering…` pattern).
- [ ] **Step 2:** `just check`; visual on a real local job. Commit `feat(video-map): live extract from moment/range`.

---

## Milestone 4 — Economics + gate + i18n

### Task 4.1: Server gate + counter (TDD)

**Files:** Modify `billing.py` (`can_extract_clip(plan) -> bool` for `plan ∈ {pro,studio}`;
`extract_requires_payment(job) -> bool` when `len(clips) >= config.extract_free_cap (=30)`), `main.py` (enforce),
`config.py` (`extract_free_cap`, `extract_clip_price`). Test `test_extract_billing.py`.

- [ ] **Step 1: Failing tests:** free/starter → gated (402/403 code); pro under cap → allowed free; pro at ≥cap →
  requires payment; charge idempotent per new `clip_id` (fresh uid + unique job_ids).
- [ ] **Step 2–4:** Implement, pass, `just check`.
- [ ] **Step 5:** Commit `feat(worker): extraction gate (Pro/Studio) + free≤30 / paywall>30 (TDD)`.

### Task 4.2: Mirror in `lib/plans.ts` + paywall/expiry UI + i18n

**Files:** Modify `apps/web/lib/plans.ts`, `ExtractButton.tsx`, `messages/{en,ru}.json`.

- [ ] **Step 1:** Mirror gate/cap in `plans.ts`; ExtractButton states: gated (upgrade CTA), free-available,
  paywall (>30), source-expired (disabled + message). All strings via `t()` in BOTH catalogs (RU idiomatic).
- [ ] **Step 2:** `just check`; commit `feat(video-map): paywall/expiry states + i18n (en+ru)`.

---

## Milestone 5 — Polish

- [ ] **5.1** Idempotency/dup-guard end-to-end (double-click → one render); a11y (keyboard seek, focus rings,
  aria on timeline); mobile stacked layout + horizontally scrollable timeline. `just check`; commit.
- [ ] **5.2** Doc sync: `docs/README.md` §"Карта видео" + one line in `docs/JOURNAL.md`; update this plan's
  checkboxes. Commit.

---

## Self-Review

- **Spec coverage:** cockpit layout → M1; coverage/●○ + lie-fix → 1.2/1.5; rVFC playhead → 1.3; timeline+drag →
  1.4; on-demand render + boundary correctness → M2+3.2; source 60-day window → 3.2; economics(≤30 free/>30
  paywall) + Pro/Studio gate → M4; i18n → 4.2; no-reprocess harness → 1.1; polish/mobile/a11y → M5. All covered.
- **Placeholder scan:** the few "confirm against code" notes are deliberate design↔code boundaries at task start,
  not blanks; `extract_clip_price` default = a config knob (finalize from `docs/BENCHMARKS`).
- **Type consistency:** `momentClipId`/`classifyMoment`/`coverageSegments` (1.2) used consistently in 1.4/1.5;
  `range_to_segment`/`segment_key` (2.1) consumed by 3.2; `ClipOut.origin` (3.1) is codegen; `can_extract_clip`/
  `extract_requires_payment`/`extract_free_cap` (4.1) mirrored in `plans.ts` (4.2).
