# Editor overhaul: per-shot reframe NLE + CapCut timeline + WYSIWYG-render + downloads

> **Spec (design)** — 2026-07-11. Branch `feat/editor-reframe-nle` (off `main` @ `63848c6`).
> Status: **DESIGN, approved on 3 core forks; awaiting spec review → writing-plans.** Local-first
> (see [[local-first-then-deploy]]): deliver locally, founder tests in-hand, then decides deploy.
>
> Reading order for the implementer: this spec → `docs/REFRAME_FPS_GRID_INVARIANT.md` (MANDATORY —
> the Δ=0 grid invariant this touches) → `docs/README.md` "Current reality" → `CLAUDE.md` rules.

---

## 1. Problem — what the founder hit, grounded in code

The per-clip editor's framing + timeline + download flows are fragmented across three
mutually-inconsistent layers. Founder QA (2026-07-11) surfaced the symptoms; recon traced each to a
concrete root cause.

**Framing state lives in three unsynchronised places:**
1. **AI plan** — `job_artifacts.reframe_regions` (migration `0013`, RPC `merge_reframe_regions`): per-shot
   `{t0,t1,mode,points[cx-trajectory],points_b,fit_style}` incl. the follow trajectory. **Read only by
   the `/reframe` PREVIEW fast-path.**
2. **User overrides** — `clip_edits.reframe_overrides: CropOverride[]` (`models.py:197-205`), range-scoped.
3. **Render** — `render_edit_to_file` (`tasks.py:286-307`) **recomputes CV from scratch**
   (`resolve_regions_accurate` → PySceneDetect+ASD+MediaPipe) and paints overrides on top; it **never reads
   `reframe_regions`** (grep-confirmed).

→ Preview and the downloaded file derive the AI base from **different sources** (baked vs fresh recompute);
"where is the AI's work saved" is genuinely ambiguous; every re-render pays full CV.

**UI writes framing in three inconsistent spots:**
- **Top Frame panel** (`FrameTab.tsx`) → `handleFrameApply` → `setCropOverride` spanning the **whole clip**
  (`ClipEditorScreen.tsx:1277-1284`). The only pan (`center`) slider and `fit_style` selector are here →
  **whole-clip only.**
- **`FitTimeline`** (per-shot strip) → `handleApplyRange` → `setCropOverride` on the selected shot range,
  but `center:null`, no `fit_style` (`ClipEditorScreen.tsx:1325-1331`) → **mode only, no per-shot pan/bg.**
  "Split here" adds a client-only cut (`userSplits`) that never persists; existing boundaries can't be
  moved/deleted.
- **`TimelineV2`** (bottom) → CapCut-style clip window exists, but **no click-to-seek**; a marker/chapter
  click calls `handleSetInterval` = a version-bumping **server round-trip** (`setPhase("saving")` →
  `flushPending` → `setClipInterval` → refetch reframe).

**Symptom → root cause map:**

| Founder complaint | Root cause (file:line) |
|---|---|
| Changed crop in one shot → applied to the whole clip | Pan (`center`) & `fit_style` exist only in the whole-clip top panel. **Per-shot pan / per-shot bg do not exist.** (`FrameTab.tsx:22`, `types.ts:262` "MVP") |
| Clicking the timeline doesn't seek immediately | `TimelineV2` has no click-seek; only drag-scrub seeks. Marker click → `handleSetInterval` network round-trip (`ClipEditorScreen.tsx:906-923`) |
| Start/length/end control worse than CapCut | Draggable window exists but slow server commit on release, no numeric fields, no snap-to-hover/marker (`TimelineV2.tsx:293-308`) |
| Does forcing tight on one shot break following on the next? / where is AI work saved | `lock_per_shot` is whole-clip; render recomputes CV each time; trim invalidates the preview fast-path (`intervals_match_default`) → the plan visibly "moves". No per-shot AI/manual provenance. |
| Constant slow re-render | Any geometry/crop/trim edit forces the FULL path: whole-source download + full CV + whole-clip re-encode (`tasks.py:262-324`). No incremental render. |
| See changes instantly, identical to editor | Captions are true WYSIWYG (same libass). Reframe is a **CSS approximation** → diverges from render (`PreviewPlayer.tsx:18-19`). |
| Wide borders: dark instead of blur | Supported (`fit_style` ∈ `blur_zoom`/`blur`/`black`, rendered by Engine A/B `stage5_render.py:289-311`) but **whole-clip only**; per-shot Wide is always `blur_zoom`. |
| Editor "download with subtitles" → file has no subs/hook (clips page is fine) | `captionedDownloadUrl` returns `bakedUrl` verbatim when not dirty (`api.ts:1281-1290`); the editor is the only surface passing `bakedUrl` (`EditorHeader.tsx:296-304`), historically seeded from the **clean** `clips/<id>.mp4`. Fixed on `main` @ `bdc554c` (downloadUrl=null, set to captioned only after render) → live repro ⇒ stale/regressed Vercel bundle or a legacy clean `render_url`. |

---

## 2. Goals / non-goals

**Goals**
- A **per-shot NLE for framing**: on each shot independently — mode (Tight/Wide), crop-pan (drag in
  preview), wide-background (blur / blur+zoom / dark), face-follow on/off; plus add / move / delete a cut.
- **One editable Frame Plan** = the single source of truth for **both** preview and render (render stops
  recomputing CV). Preview == download by construction.
- **No mid-edit waiting**: the live preview is the working surface; a single fast render happens **only on
  export/download**.
- **CapCut-grade timeline**: instant click-to-seek, drag-scrub, numeric in/out/duration, snap to hover
  line / markers / shot boundaries.
- **Explicit AI↔manual model**: per-shot provenance so a manual edit never leaks to neighbours, and it's
  clear where the AI's tracking/following is stored and editable.
- **Downloads fixed & unified**: kill the "editor captioned → clean file" bug robustly; every download
  surface offers the full, correct format set; on-demand renders move OFF `quip-api` (OOM).
- **Net-lower server load** and **zero regression** to the Δ=0 reframe grid, money paths, and existing clips.

**Non-goals (YAGNI / deferred)**
- Split-screen mode (removed in MVP; legacy split coerced to `fit` — keep as-is).
- Big-video segment fan-out (`SEGMENT_FANOUT`, dormant/OFF in `main`) — the plan must be *compatible* with
  it, but wiring it is out of scope (see [[big-video-and-dedup-roadmap]]).
- Client-side pixel-exact WebGL render (fork rejected by founder).
- Worker-side `JobError` i18n and editor i18n tails (separate track).

---

## 3. Core: the Frame Plan (single source of truth)

A per-clip, **source-absolute** document that materialises the AI's per-shot decision and is then edited
in place. It is a strict superset of today's `TrackRegion` (which already serialises via
`_region_to_dict`/`_from_dict`, `reframe_cache.py:199-231`).

```
FrameShot:
  src_start: float          # SOURCE seconds, frame-snapped: round(t*fps)/fps  (§ invariant)
  src_end:   float          # SOURCE seconds, frame-snapped
  mode:      "fill" | "fit"                    # tight | wide (rendered vocab unchanged)
  fit_style: "blur_zoom" | "blur" | "black"    # per-shot wide background
  follow:    bool                              # fill only: static locked crop vs face-follow
  pan:       {cx: float} | null                # per-shot crop centre (null = auto/lock-to-face)
  track:     [{t: float, cx: float}, ...]      # follow trajectory (THE AI's tracking, editable)
  origin:    "ai" | "manual"                   # provenance → the neighbour-safety guarantee

FramePlan:
  fps:      float           # exact native source fps (NOT rounded — see fps-flash postmortem)
  shots:    [FrameShot, ...]  # contiguous over the clip's default source span
  version:  int             # optimistic lock, reuses ClipEdit.version discipline
```

**Three design decisions inside:**

1. **Source-absolute time base** (today regions are interval-relative). Trimming then just selects a
   sub-span of the plan; **framing no longer reshuffles on trim** (the current failure: trim → fast-path
   miss → recompute → plan "moves"). Render/preview derive interval-relative regions via the *existing*
   aligned-frame math (`aligned=round(src*fps)/fps`, `flatten_timeline` anchor) so the Δ=0 grid holds.

2. **`TrackRegion` is reused, not replaced.** `FrameShot` ≈ `TrackRegion` + `{follow, origin}` at
   shot granularity. Serialisation extends `_region_to_dict`/`_from_dict`. **Storage:** the plan lives in
   `clip_edits` (user-editable, versioned — the same home as `reframe_overrides` today), **materialised/seeded
   from `job_artifacts.reframe_regions`** (the AI batch output stays as the seed + preview back-compat). See §9.

3. **Provenance (`origin`).** Editing shot *i* flips it to `manual`; shots `≠i` are untouched. Because CV
   is **not** recomputed on edit and the plan is per-shot, **neighbouring shots' following cannot change** —
   this is the founder's guarantee, enforced structurally, not by convention. A future AI re-run mutates
   only `ai` shots.

**Where the AI's work lives (answering the founder directly):** the tracking/following IS
`FrameShot.track` + `pan` + `mode`, materialised into the plan and editable. It is not "somewhere in CV" —
it's a first-class, per-shot, editable field with a provenance tag.

---

## 4. Render pipeline: read-the-plan + defer-to-export + prebake

**4a. Render reads the plan (no CV).** The single seam is `render_edit_to_file`
(`tasks.py:286-307`):

```
BEFORE:  regions = resolve_regions_accurate(source, intervals, overrides, ... CV ...)
AFTER:   regions = plan_to_regions(frame_plan, intervals, fps)   # pure deserialize + re-anchor
         render_timeline(out, "source.mp4", intervals, regions, ...)   # UNCHANGED
```

`plan_to_regions` mirrors `regions_from_persisted` (`reframe_cache.py:333`, which already produces a
result "идентичный resolve_regions_accurate, только без CV") but is source-time-aware. `render_timeline`
is untouched → the grid math is untouched. **No-silent-fallback (rule #8):** the only case needing CV is
a trim that extends the clip *beyond* the plan's default-covered source span — handled by an explicit,
logged one-shot CV top-up on the uncovered sub-span (with a visible indicator), never a silent recompute.

**4b. Defer render to export.** Remove the mid-edit "Render" gate as the working loop. The live preview is
authoritative while editing. A render is triggered **on download/export only**, and (per §4c) is prebaked
so "Download" is instant. Result: no waiting after any framing/trim/pan edit.

**4c. Prebake + OOM-safe delivery (reuse L1/L2).** The export render runs on the **render Job/fan-out**
(`render_clip_edit_job` / `reframe_render_clip`), **never in-process on `quip-api`** (the tmpfs=RAM OOM
lesson, [[instant-captioned-download-2026-07-10]]). It writes both artifacts — clean `clips/<id>.mp4` and
`clips/<id>_captioned.mp4` — and downloads 302-redirect to the CDN object (existing L1). A debounced
prebake after edits keeps the baked artifact warm (existing L2 `PREBAKE_CAPTIONED`).

**Net effect on the "constant slow render" pain:** CV removed from the hot path + render deferred + prebaked
⇒ the founder never waits mid-edit, and the final download is a CDN redirect.

---

## 5. Preview = the WYSIWYG surface

The editor already plays `preview.mp4` (≤720p source proxy) looped in `[outerStart,outerEnd)` with
imperative `applyCrop` inside `requestVideoFrameCallback` + libass captions (`PreviewPlayer.tsx:79-112`,
222-270). It already reads the plan (via `/reframe`). Changes:

- Preview reads **the Frame Plan** directly (same object the render consumes) → determinism by construction.
- Close the residual gap: match the CSS look to the ffmpeg filtergraph (crop math identical; `black`/`blur`/
  `blur_zoom` visually matched to `stage5_render._region_filter`; frame-accurate cut already via rVFC).
- Captions stay pixel-identical (same ASS/libass).
- The only accepted divergence is proxy resolution + gaussian-blur exactness (cosmetic). Everything geometric
  is 1:1 because both sides deserialize one plan.

---

## 6. Per-shot NLE UI

**6a. Shot inspector** (`FitTimeline` → real shot editor, inside the Frame tab). Select a shot → edit
*that shot*: Mode (Tight/Wide), Crop-pan (**drag the crop frame directly in the preview** → writes
`pan.cx`; slider as fallback), Follow on/off, Wide-background (blur / blur+zoom / dark). Each control writes
one `FrameShot` and flips `origin=manual`; preview updates instantly.

```
┌ SHOTS ───────────────────────────────────────────────────────────┐
│ │ shot1 tight ││  shot2 WIDE  ││ shot3 tight* ││ shot4 tight │    │  * = manual
│   ┃playhead        ✂ Split        ⇄ move boundary     ✕ merge     │
├─ Inspector · SHOT 3 (01:31–01:44) ──────────────── origin: manual │
│  Mode: (Tight ●)(Wide ○)   Follow face: [on|off]                  │
│  Crop: ⟵ drag in preview, or cx [====●====]                       │
│  Wide bg: (blur)(blur+zoom)(dark ●)                               │
└───────────────────────────────────────────────────────────────────┘
```

**6b. Cut editing** (first-class). Split (add a boundary at the playhead), move a boundary, delete/merge
adjacent shots — all **frame-snapped** through `_snap(t,fps)` (`reframe_cache.py:76`) so no sub-frame sliver
and the grid holds. Split/merge/repaint are pure list mutations on `shots` (TDD). A user-moved boundary onto
a non-content-cut is a *deliberate* hard framing switch (not the 1-frame flash bug), allowed as long as
frame-snapped.

**6c. Preview↔plan bindings.** Dragging the crop in the preview mutates the active shot's `pan`; the
timeline playhead selects the shot; per-shot changes never touch other shots.

---

## 7. CapCut-grade timeline

Port the cockpit-grade scrubber (`components/…MapTimeline`, already built on `feat/video-map-cockpit` from
`components/editor/TimelineV2.tsx`; see [[video-map-cockpit-redesign]]) into the clip editor:

- **Click = instant seek** (client-only `video.currentTime`, no server round-trip — fixes the lag). Drag =
  scrub, coalesced 1×/rAF.
- **Trim in/out/duration**: draggable window + numeric `start/end/duration` fields + **snap** of in/out to
  the hover line, markers, and shot boundaries. Trim is instant in preview; the interval commit does **not**
  force a reframe refetch/render (plan is source-time, so trim doesn't reshuffle framing).
- **Scrub/Select toggle**, Ctrl+wheel zoom, pan, hover tooltip, coverage/markers band.
- Same treatment for the per-shot framing timeline (seek + boundary drag).

```
┌ TIMELINE (whole source) ──────────── [Scrub|Select]  [− zoom +] ─┐
│  ▁▂▃▅▇▅▃  waveform   ● moment   ▓clipped ░untapped                │
│        ┃hover 01:12        ╎playhead                             │
│     ╔═══ clip window (drag body / ◀▶ grips) ═══╗                 │
│     ║ [start 01:05.2] [end 02:03.8] [00:58.6]  ║                 │
└─────╚═════════════════════════════════════════╝─────────────────┘
```

---

## 8. Download overhaul (bug fix + unify formats + OOM-harden)

**8a. Fix the captioned→clean bug (robust, defense-in-depth).**
- Frontend: `captionedDownloadUrl` (`api.ts:1281-1290`) **stops trusting `bakedUrl`** and always returns
  `clipCaptionedExportUrl(...)`. That endpoint already L1-302-redirects to the baked **captioned** CDN
  object (`main.py:1505-1509` → `storage.clip_read_url(..., variant="captioned")`) → instant *and* always
  correct. The `bakedUrl` shortcut gave no speed, only the clean-file risk. (Alt: trust `bakedUrl` only if
  its key ends `_captioned.mp4`.)
- Verify the live Vercel bundle contains `bdc554c` (current `main` is already correct) → the live repro is a
  stale/regressed deploy; a frontend redeploy is part of remediation.

**8b. Unify formats across every surface.** One shared download menu offering the full set consistently on
Editor / Clips page / Results: **captioned mp4, clean mp4, SRT, VTT, EDL, FCPXML, package.zip.** Wire the
existing-but-orphaned **VTT** endpoint (`main.py:1777`) into the menus.

| Surface | Today | After |
|---|---|---|
| Editor `ExportMenu` | captioned (bug vector), clean, SRT, zip, EDL, FCPXML | full set, captioned via L1 endpoint (no `bakedUrl`) + VTT |
| Clips page `ClipCard` | full set (ok) | + VTT, shared component |
| Results `DownloadAllMenu` | captioned/clean/SRT | + VTT, shared component |

**8c. OOM-harden on-demand renders.** `export/clean.mp4` (`main.py:1466`) and the captioned fallback
(`main.py:1515`) still `render_edit_to_file(...)` **in-process on `quip-api`**. Fold into §4 defer+prebake:
the export render (clean + captioned) runs on the render Job and both are served by CDN redirect. `quip-api`
stops rendering/streaming large mp4s. `package.zip` (text/manifests in `BytesIO`, `main.py:1684/1796`) is
light — keep, but note it in the OOM ledger.

---

## 9. Migration & compatibility

- **Materialise:** on first editor open of a clip without a Frame Plan, build it from persisted
  `reframe_regions` (source-time re-anchor). If regions are absent/stale, one bounded CV pass builds it (then
  cached). New batch jobs write the plan directly (extend `build_persist_payload`).
- **`reframe_regions` vs Frame Plan:** the plan supersedes regions as the editable truth; keep writing
  regions for back-compat/preview until fully cut over. **Cache-clear runbook still applies**
  (`update public.job_artifacts set reframe_regions = null;` after any reframe-logic deploy) +
  local `DELETE FROM clip_edits WHERE job_id=?` before local render verification (stale-edit gotcha,
  [[clip-quality-reframe-night-2026-07-07]]).
- **`reframe_overrides` back-compat:** existing overrides map onto per-shot edits (`origin=manual`) so
  already-edited clips keep their framing.
- **Type contract:** `FrameShot`/`FramePlan` are `models.py` changes → **`just types`** regenerates
  `packages/shared` (never hand-edit).
- **Segment fan-out compat:** source-time plan is per-source, so it composes with per-clip segment
  extraction (dormant `SEGMENT_FANOUT`) — the plan indexes source time, segments carry `shift_segment`.

---

## 10. DevOps / load / no-regression

- **Load: net LOWER.** Removing per-edit CV (PySceneDetect+ASD+MediaPipe) off the render hot path + deferring
  render to export + prebake = fewer, cheaper render jobs and zero new per-edit server load (preview is
  client CSS). One-time materialise CV per legacy clip is bounded + cached.
- **Cloud Run mapping preserved:** export render stays on `quip-pipeline`/`quip-render` Jobs; `quip-api`
  (4Gi service) stops in-process mp4 renders (§8c) → removes a live OOM vector.
- **Invariants guarded:** Δ=0 grid (§ frame-step verification), money paths untouched, `intervals_match_default`
  + L1/L2 prebake reasoned about explicitly.
- **No new heavy dependency;** no new always-on container.

---

## 11. Scenario coverage (founder's flow + edge cases)

| Step | New pipeline behaviour |
|---|---|
| See wrong Wide → force Tight on that shot | shot inspector → `mode=fill`, `origin=manual`; instant preview |
| Set crop just here | drag in preview → `shots[i].pan.cx`; per-shot only |
| Next shot's following still ok | neighbours `ai`, CV not recomputed → unchanged (structural guarantee) |
| Boring clip start → trim, align to hover line | click=instant seek; drag in-point snaps to hover/marker; `source_intervals` update, plan not reshuffled |
| AI missed a transition → cut it myself | Split (frame-snapped) → 2 shots, assign modes |
| Dark borders instead of blur | shot inspector → Wide bg = dark (per-shot `fit_style`) |
| Don't want to wait for renders | nothing renders mid-edit; one fast prebaked render on Download |
| Download with subtitles | always the captioned L1 endpoint → subs+hook present |

**Edge cases handled:** trim-beyond-plan (explicit CV top-up, indicated); ≠25/30 fps (all boundaries via
`_snap`, verified frame-step on real video); legacy clips (materialise from regions or one CV); big source
(render on Job w/ 32Gi ephemeral-disk, not api); free watermark (burned at export render as today);
user-moved boundary off a content cut (allowed, deliberate, frame-snapped).

---

## 12. Testing & verification

- **TDD (pure logic):** plan mutations (split/merge/repaint/pan/snap), `plan_to_regions` re-anchoring,
  source→interval time mapping, override→plan migration, download-URL resolution.
- **Δ=0 grid (MANDATORY, cannot be unit-tested):** frame-step the REAL render at every fill↔fit seam on a
  ≠25fps source (per `REFRAME_FPS_GRID_INVARIANT.md`; `verify_grid_fix.py` Δ=0 does NOT catch content-splice
  misalignment). Reuse harnesses `ab_reframe.py`/`ab_edit.py`/`detect_mode.py`.
- **Preview==render:** assert `plan_to_regions` == `regions_from_persisted` for an unedited clip; visual A/B
  preview vs downloaded mp4 at seams.
- **Download E2E:** editor captioned download contains subs+hook (the bug); every format from every surface.
- **Gate:** `just check` green before every commit (stop `next dev` first — Turbopack `.next/dev/types`
  breaks tsc, [[night-run-2026-07-06-icp-repositioning]]).

---

## 13. Phased plan (local-first; via workflows/subagents + TDD)

Strict file boundaries per agent; orchestrator commits + runs `just check`.

- **P0 — Frame Plan model + `plan_to_regions` (render reads plan).** `models.py` (+`just types`),
  `reframe_cache.py`, `tasks.py:286` seam. **TDD + Δ=0 frame-step.** The spine; highest risk.
- **P1 — Per-shot inspector** (mode/pan-drag/bg/follow) on `FitTimeline` + `PreviewPlayer` drag-to-pan.
- **P2 — CapCut timeline** (click-seek, snap-trim, numeric) — port `MapTimeline`.
- **P3 — Cut editing** (split/move/merge as first-class plan mutations).
- **P4 — Defer-render-to-export + prebake** + preview-look fidelity pass.
- **P5 — Download overhaul.** §8a bug fix + §8b unified formats + VTT are **independent & low-risk → ship
  first as a quick win** (no dependency on P0–P4). §8c OOM-harden depends on **P4** (defer+prebake).

**Deploy note:** after any reframe-logic deploy, clear `reframe_regions` (§9). Frontend deploy is manual
(`vercel deploy --prod`), worker via Cloud Run (see [[deploy-runbook-2026-07-04]]). Nothing deploys without
explicit founder go.

---

## 14. Open questions / risks

1. **Δ=0 re-anchoring** — the one true risk; mitigated by source-time snapping + mandatory frame-step. If a
   seam misaligns, fall back to keeping render on `resolve_regions_accurate` for trimmed clips only (preview
   still plan-driven) — a documented, non-silent degrade.
2. **Follow trajectory editing granularity** — do users edit `track` keyframes directly, or only toggle
   follow on/off + set a static `pan`? Proposed: on/off + static pan in v1; keyframe editing later.
3. **Prebake trigger cadence** — debounce window vs cost; reuse L2 knobs.
4. **Frontend stale-deploy** — confirm the live bundle (the download bug may already be fixed in code).
