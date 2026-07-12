# Speaker Focus Reframe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline, batch with eyes-on checkpoints). Steps use `- [ ]` tracking. Spec: `docs/superpowers/specs/2026-07-13-speaker-focus-reframe-design.md`. Research: `docs/archive/2026-07-13-active-speaker-reframe-research.md`.

**Goal:** In multi-person single-camera clips, crop TIGHT on the active speaker and switch between speakers as a HARD CUT (virtual 2nd camera) timed to diarization turns, with wide fallback — default ON, wide as opt-in.

**Architecture:** New pure module `pipeline/speaker_switch.py` (turns → lane↔cluster binding → greedy switch scheduler) feeds `plan_regions`, which sub-divides a real shot into per-speaker fill regions + wide fallbacks. Render is unchanged (region boundary already = hard `concat` cut). Diarization (`Word.speaker`, Deepgram) + per-frame Light-ASD are fused; nothing swaps the ASD model.

**Tech Stack:** Python 3, numpy, existing Light-ASD (`app/asd`), MediaPipe face + IOU tracks (`stage3_speaker`), PySceneDetect, ffmpeg (Engine B render). Tests: pytest (`services/worker/tests/unit`). uv env, CPU-only.

## Global Constraints (verbatim from spec)
- **Grid Δ=0:** every synthetic boundary = `round(t*fps)/fps` in NATIVE fps; test on non-integer fps. `docs/REFRAME_FPS_GRID_INVARIANT.md`.
- **Backward-compat:** `words=None` / `reframe_mode` absent → planner byte-identical to today.
- **No silent fallbacks** (CLAUDE.md): off-screen/ambiguous → explicit WIDE region + debug reason, never a silent wrong crop.
- **TDD on pure logic** first (turns, binding, scheduler, IoU, snap).
- **Local run:** PATH-refresh + `Set-Location services\worker`; harness `.scratch\reframe_harness.py`; tests `uv run pytest tests/unit/... -q`; worker gate `just lint-py; just typecheck-py; just test-unit`. CPU-only → segments 30–90 s. Deepgram key present (Russian → diarize works).
- **Default reframe_mode = "speaker"** (tight); `"wide"` = opt-in. Per-user via `jobs` col + `ensure_edit` seed (like `remove_pauses`).
- **Cache:** after planner change, `update public.job_artifacts set reframe_regions = null;` post-deploy (deploy = founder's call later).
- **Test asset:** `C:\Users\user\Downloads\Telegram Desktop\JIQwWepYgMyXUW9xg9Sw+zcLr7PvvXDY.mp4` (1920×1080, 30fps, 45.8min, 1 locked cam, 2 separated speakers).

## File Structure
- **Create** `services/worker/app/pipeline/speaker_switch.py` — `SpeakerTurn`, `LaneBinding`, `SubRegion`, `build_turns`, `bind_lanes`, `plan_switches`, `fill_crop_xrange`, `crop_iou_x`. All PURE.
- **Modify** `services/worker/app/asd/scorer.py` — `score_track` also returns per-frame speak array.
- **Modify** `services/worker/app/pipeline/asd_reframe.py` — carry `speak_frames` into `SpeakerTrack`.
- **Modify** `services/worker/app/pipeline/stage3_reframe.py` — `SpeakerTrack.speak_frames`; `resample_track` resamples it; `plan_regions`/`reframe_segment` accept `words`+`reframe_mode`, sub-divide shot via `plan_switches`.
- **Modify** `services/worker/app/config.py` — switch knobs.
- **Create** `services/worker/tests/unit/test_speaker_switch.py`.
- **Create** `.scratch/speaker_focus_debug.py` — P0 attribution dump + annotated frames.
- **Create** `.scratch/speaker_focus_render.py` — P1 render on the test video (words+mode → reframe_segment → render_clip).
- **Deferred (P1-prod)** migration `jobs.reframe_mode`, `ensure_edit`/`default_clip_edit`, `models.ClipEdit.reframe_mode`, `apps/web` toggle + i18n.

---

## PHASE P0 — prove attribution (read-only, no product render)

### Task 1: Per-frame ASD scores from `score_track`
**Files:** Modify `app/asd/scorer.py`; Test `tests/unit/test_asd_reframe.py` (or new `test_scorer.py`).
**Interfaces — Produces:** `score_track(...)` returns `(mean: float, per_frame: np.ndarray)` OR keeps returning mean and adds `score_track_frames(...) -> np.ndarray`. Choose the additive form to avoid breaking callers: keep `score_track` returning the scalar; add `per_frame` as a second return via a new keyword or a sibling. **Decision:** change `score_track` to return `np.ndarray` (per-frame, length = n face frames) and compute the scalar mean at the call site in `asd_reframe`. Update the one caller. (Scalar was only consumed there.)

- [ ] Step 1: Read `app/asd/scorer.py:score_track` fully; find where per-frame scores exist before the `_DURATIONS` averaging.
- [ ] Step 2: Write failing test: per-frame array length == number of input face frames; `mean(per_frame)` ≈ old scalar (within 1e-3) on a synthetic/short real track.
- [ ] Step 3: Run → fail.
- [ ] Step 4: Implement: return the per-frame averaged-over-durations array (do NOT collapse to scalar inside); caller means it.
- [ ] Step 5: Update caller in `asd_reframe.py` (Task 2). Run test → pass.
- [ ] Step 6: `uv run pytest tests/unit/test_asd_reframe.py -q`; commit `feat(asd): expose per-frame speak scores from score_track`.

### Task 2: Carry `speak_frames` into `SpeakerTrack` + resample
**Files:** Modify `app/pipeline/asd_reframe.py`, `app/pipeline/stage3_reframe.py` (`SpeakerTrack` dataclass ~:141, `resample_track` ~:426); Test `tests/unit/test_stage3_reframe.py`.
**Interfaces — Produces:** `SpeakerTrack` gains `speak_frames: tuple[float,...]` (per-frame @ track fps; empty tuple when ASD skipped/`_SILENT`). `resample_track(t, src_fps, dst_fps)` resamples `speak_frames` with the same nearest-by-time mapping as `cx`.
- [ ] Step 1: Failing test: a `SpeakerTrack` with `speak_frames` of length N, resampled 25→30 fps, has `speak_frames` length matching resampled `cx`, nearest-by-time values preserved at endpoints.
- [ ] Step 2: Run → fail (field missing).
- [ ] Step 3: Add field (default empty tuple for back-compat); populate in `asd_reframe` where `SpeakerTrack` is built (per-track per_frame from Task 1; when `should_score_asd` skips → empty). Resample in `resample_track` alongside `cx`.
- [ ] Step 4: Run → pass. Ensure existing `test_asd_reframe`/`test_stage3_reframe` still green.
- [ ] Step 5: Commit `feat(reframe): SpeakerTrack.speak_frames (per-frame ASD, resampled)`.

### Task 3: `build_turns` (diarization → speaker turns)  [PURE]
**Files:** Create `app/pipeline/speaker_switch.py`; Test `tests/unit/test_speaker_switch.py`.
**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class SpeakerTurn:
    speaker: int          # diarization cluster id
    t_start: float        # clip-relative sec (first word onset)
    t_end: float          # clip-relative sec (last word end)
    n_words: int

def build_turns(words: Sequence["Word"], clip_start: float, clip_end: float,
                *, merge_gap: float = 0.4) -> list[SpeakerTurn]:
    """Group consecutive words by .speaker into turns (clip-relative times).
    Words with speaker=None inherit the current speaker. Same-speaker turns
    separated by a gap < merge_gap are merged. PURE. Empty/no-labels -> []."""
```
- [ ] Step 1: Failing tests: (a) 3 words spk0, 3 words spk1 → 2 turns with right times; (b) a lone spk1 word between spk0 words with tiny gap → still 1 spk0 turn if the interjection merges? NO — build_turns is raw grouping; interjection = its own turn (min-hold filtering is in plan_switches). Assert 3 turns; (c) speaker=None inherits previous; (d) empty/all-None → `[]`; (e) same-speaker split by 0.2s gap (< merge_gap) → merged into 1.
- [ ] Step 2: Run → fail (module/func missing).
- [ ] Step 3: Implement `build_turns` + `SpeakerTurn`. Times clip-relative (`w.start - clip_start`), clamped to `[0, clip_end-clip_start]`.
- [ ] Step 4: Run → pass.
- [ ] Step 5: Commit `feat(reframe): build_turns from diarization`.

### Task 4: `bind_lanes` (cluster ↔ face-lane)  [PURE]
**Files:** Modify `app/pipeline/speaker_switch.py`; Test same.
**Interfaces — Produces:**
```python
def bind_lanes(turns: Sequence[SpeakerTurn], tracks: Sequence["SpeakerTrack"], fps: float,
               *, conf_min: float = 0.6) -> dict[int, int | None]:
    """For each turn, the lane (track index) with the highest mean ASD speak
    over that turn's native frames casts one vote for turn.speaker. Per cluster,
    winner = argmax votes; confidence = winner_votes / total_votes; below
    conf_min -> None (off-screen / ambiguous). PURE. tracks carry speak_frames
    (native fps) + f0/f1. Ignores turns where all lanes have no ASD data."""
```
- [ ] Step 1: Failing tests: (a) 2 lanes, lane0 speaks during spk0 turns, lane1 during spk1 → `{0:0, 1:1}`; (b) one noisy turn votes wrong but majority correct → still right (robustness); (c) a cluster whose turns have no lane with ASD data (all silent/off-screen) → `None`; (d) conf below `conf_min` (50/50 split) → `None`.
- [ ] Step 2: Run → fail.
- [ ] Step 3: Implement. Per turn: for each track overlapping the turn frames, mean of `speak_frames` on the intersection; skip tracks with empty `speak_frames`/all-sentinel; argmax lane votes. Aggregate votes per cluster.
- [ ] Step 4: Run → pass.
- [ ] Step 5: Commit `feat(reframe): bind_lanes cluster<->face via per-turn ASD voting`.

### Task 5: P0 attribution proof on the test video  (EYES-ON GATE)
**Files:** Create `.scratch/speaker_focus_debug.py`.
- [ ] Step 1: Script: cut a 60–90s segment of the test video where both talk (e.g. `--start 800 --end 880`); extract 16k wav; `transcribe()` (Deepgram diarize) → words; `score_tracks_in_segment()` → tracks (with speak_frames, whole segment as one shot, `cuts=()`); `build_turns` + `bind_lanes`.
- [ ] Step 2: Print a table: turn# | speaker | t_start–t_end | n_words | bound_lane | lane mean_cx | (left/right). Also dump per-turn: which lane won and by what margin.
- [ ] Step 3: Render annotated proof frames: sample 1 frame per turn at its midpoint, draw each track bbox + mark the bound lane; save PNGs.
- [ ] Step 4: **EYES-ON:** confirm the bound lane = the person actually talking (left man vs right woman) across ≥6 turns. If wrong → debug binding before P1. Record findings in a short note.

---

## PHASE P1-core — scheduler + integration + render

### Task 6: `fill_crop_xrange` + `crop_iou_x`  [PURE]
**Files:** Modify `app/pipeline/speaker_switch.py`; Test same.
**Interfaces — Produces:**
```python
def fill_crop_xrange(cx: float, crop_w_frac: float) -> tuple[float, float]:
    """9:16 fill window normalized x-range, clamped to [0,1]."""
def crop_iou_x(a: tuple[float,float], b: tuple[float,float]) -> float:
    """1-D IoU of two full-height crop windows (fill crops share full height)."""
```
- [ ] Step 1: Failing tests: separated crops (0.06–0.38 vs 0.49–0.81) → IoU 0.0; identical → 1.0; half-overlap exact value; clamp at edges.
- [ ] Step 2–4: implement, run, pass.
- [ ] Step 5: Commit `feat(reframe): fill crop x-range + 1D IoU helpers`.

### Task 7: `plan_switches` greedy scheduler  [PURE — the crux]
**Files:** Modify `app/pipeline/speaker_switch.py`; Test same.
**Interfaces — Consumes:** `SpeakerTurn`, `bind_lanes` output, per-lane cx (from tracks), `fill_crop_xrange`/`crop_iou_x`. **Produces:**
```python
WIDE = -1
@dataclass(frozen=True)
class SubRegion:
    t0: float; t1: float; target: int   # lane index, or WIDE

def plan_switches(turns, binding, lane_cx, crop_w_frac, shot_t0, shot_t1, fps, cfg) -> list[SubRegion]:
    """Greedy lookahead scheduler. lane_cx: dict[lane]-> callable(t0,t1)->mean cx.
    Rules (spec §6.5/§6.6): only switch when the incoming speaker's bound lane
    holds >= cfg.min_hold; hysteresis (no cut-back within min_hold); IoU gate on
    fill<->fill only (wide<->fill exempt) — IoU>iou_beta -> WIDE; unbound
    cluster / 3+ lanes / low conf -> WIDE; short interjection -> linger (no
    switch). Boundaries snapped to first-word onset pulled into pre-onset pause,
    then round(t*fps)/fps. Regions >= min_hold; tiny leftovers absorbed. PURE."""
```
- [ ] Step 1: Failing tests (synthetic turns): (a) clean A(3s)→B(3s) → two fill regions, boundary frame-snapped; (b) A→(0.4s "yeah" B)→A → single A region (interjection < min_hold ignored, linger); (c) two fill crops with high IoU (close speakers) → WIDE for the 2nd; (d) unbound cluster (binding None) → WIDE; (e) boundary lands exactly on `round(t*fps)/fps` for fps=29.97 and 30; (f) rapid alternation < min_hold → lingers on last committed target (documented v1 behavior).
- [ ] Step 2: Run → fail.
- [ ] Step 3: Implement the scheduler.
- [ ] Step 4: Run → pass; add fps 24/25/29.97/30/60 grid assertions.
- [ ] Step 5: Commit `feat(reframe): plan_switches greedy speaker-cut scheduler`.

### Task 8: Integrate into `plan_regions` (sub-divide shot)
**Files:** Modify `app/pipeline/stage3_reframe.py` (`plan_regions` ~:697, `reframe_segment` ~:1267); Test `tests/unit/test_stage3_reframe.py`.
**Interfaces — Consumes:** `plan_switches`. `plan_regions` gains `words: Sequence[Word] | None = None`, `reframe_mode: str = "auto"`. When `reframe_mode=="speaker"` AND a shot has ≥2 bound lanes → run `plan_switches` on that shot's `[f0,f1)` and emit one `TrackRegion` per `SubRegion` (fill target lane → existing `_fill_points_for_target` on sub-range; WIDE → fit). Boundaries stay on `round(t*fps)/fps`. `merge_short_regions` NOT applied to these (own min-hold already enforced) — pass through / flag. `words=None` or `reframe_mode!="speaker"` → **byte-identical** to today.
- [ ] Step 1: Failing test: with `reframe_mode="speaker"` + words giving 2 clean turns on a 2-lane shot → 2 regions with different fill targets, boundary on real cut grid; with `words=None` → identical to current `plan_regions` output (snapshot).
- [ ] Step 2: Run → fail.
- [ ] Step 3: Implement branch inside the per-shot loop; keep `_is_wide_shot`/`_pick_target`/silence-gate for the default path untouched.
- [ ] Step 4: Run → pass; **verify back-compat**: full `tests/unit/test_stage3_reframe.py` green.
- [ ] Step 5: Commit `feat(reframe): plan_regions speaker-switch sub-division (opt-in, back-compat)`.

### Task 9: Thread `words` + `reframe_mode` through `reframe_segment` → run/tasks
**Files:** Modify `app/pipeline/stage3_reframe.py:reframe_segment` (params + pass to `plan_regions`; build turns/binding inside using tracks already computed); `run.py`/`tasks.py` pass `words` (from `store.load_transcript_words`) + `reframe_mode` from edit.
- [ ] Step 1: `reframe_segment(..., words=None, reframe_mode="auto")`; inside, after tracks computed, `build_turns`+`bind_lanes`, pass to `plan_regions`.
- [ ] Step 2: Wire `run.py:render_one_clip` + `tasks.py:render_edit_to_file` to pass words+mode (guard: default path unchanged when mode!="speaker").
- [ ] Step 3: `uv run pytest tests/unit/ -q` (reframe subset) green.
- [ ] Step 4: Commit `feat(reframe): wire diarization + reframe_mode into reframe_segment`.

### Task 10: Config knobs
**Files:** Modify `app/config.py`.
- [ ] Add: `switch_min_hold_sec=1.7`, `switch_target_hold_sec=3.5`, `switch_iou_alpha=0.15`, `switch_iou_beta=0.30`, `switch_establish_sec=0.0`, `switch_turn_merge_gap=0.4`, `switch_bind_conf_min=0.6`, `switch_onset_pause_min=0.15`. Thread into `cfg` used by `plan_switches`.
- [ ] Commit `feat(config): speaker-switch reframe knobs`.

### Task 11: Render on the test video (EYES-ON GATE + Δ=0)
**Files:** Create `.scratch/speaker_focus_render.py` (or extend `reframe_harness.py` to accept `--words`/`--reframe-mode`).
- [ ] Step 1: On a 60–90s both-talking segment: transcribe→words; `reframe_segment(..., words, reframe_mode="speaker")`; `render_clip` (Engine B) → `render.mp4` + proof frames at each switch boundary.
- [ ] Step 2: Also render `reframe_mode="wide"` (baseline) for before/after.
- [ ] Step 3: **EYES-ON purpose-built check (spec §10):** (1) crop is on the SPEAKER; (2) cut lands on the speaker change; (3) no ping-pong; (4) cut reads as 2nd camera, not a slide. Grid: assert every region boundary `|t0*fps-round(t0*fps)|≈0`.
- [ ] Step 4: If issues → systematic-debugging on the specific failure (attribution vs timing vs framing).

### Task 12: Worker gate green + branch commit
- [ ] `just lint-py; just typecheck-py; just test-unit` green (fix ruff-format re-adds).
- [ ] Commit any cleanup. Do NOT run web half unless `apps/web` touched.

---

## PHASE P1-prod — toggle plumbing (deferred; do if P1-core solid, else document as remaining)

### Task 13: `reframe_mode` persistence
- [ ] Migration `jobs.reframe_mode TEXT DEFAULT 'speaker'` (+ sqlite `_ensure_column`); `models.ClipEdit.reframe_mode: str = "speaker"`; `ensure_edit`/`default_clip_edit(pref_reframe=…)` seeds from owner's job; thread into `reframe_segment` calls. `"wide"` → existing `mode_setting="fit"`. TDD on default_clip_edit seeding.
### Task 14: Frontend toggle + i18n
- [ ] `SourceForm` toggle «Кадр: за спикером / широкий»; proxy through createUploadJob chain; i18n keys in BOTH `messages/{en,ru}.json`. (Requires node → run web gate.)

---

## Morning variants (for founder review)
After P1-core renders cleanly, produce side-by-side renders on the test video for the genuine forks:
1. **min-hold** 1.2 vs 1.7 vs 2.5 s (pacing).
2. **establishing wide** 0 vs 0.8 s open.
3. **framing** identical fill scale vs A/B scale-differentiated (anti «same-photo»).
Present as before/after clips + a short note; founder picks.

## Self-review notes
- Spec coverage: §6.1–6.9 → Tasks 1–10; §10 DoD eyes-on → Tasks 5,11; §6.9 toggle → Tasks 13–14; §8 phases mapped. ✓
- Back-compat guaranteed by `words=None`/`reframe_mode!="speaker"` in Tasks 8–9 (snapshot test). ✓
- Grid Δ=0 asserted in Tasks 7,11. ✓
- Naming consistent: `speak_frames`, `SpeakerTurn`, `SubRegion`, `WIDE=-1`, `plan_switches`, `bind_lanes`, `build_turns`, `fill_crop_xrange`, `crop_iou_x`, `reframe_mode`. ✓
