# P0 — Render reads the Frame Plan (no CV on the authoritative path) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make `render_edit_to_file` read the persisted batch reframe plan (no CV recompute) whenever it is
authoritative — locked, untrimmed, and fps-consistent — falling back to the existing CV path otherwise, with
pixel-identical (Δ=0) output and preview==render on that path.

**Architecture:** The persisted `job_artifacts.reframe_regions` payload already IS the plan (a list of
`TrackRegion` dicts + default span). Preview already renders it without CV via `regions_from_persisted`. This
phase adds a pure `plan_to_regions(...)` that returns render-ready `list[list[TrackRegion]]` from that payload
(or `None` when not authoritative), and wires it into the one render seam (`tasks.py`). To keep the Δ=0 grid
safe, the plan gains a stored `fps`; plan-read is used only when it matches the render's re-probed `src_fps`.

**Tech Stack:** Python 3.12, pydantic v2 models, frozen dataclasses (`TrackRegion`/`TrackPoint`), pytest,
ffmpeg render (`render_timeline`). PowerShell + PATH refresh + `uv run` for tests (Bash has no `just`/`uv`).

## Global Constraints

- **Reframe grid invariant Δ=0** — read `docs/REFRAME_FPS_GRID_INVARIANT.md` before touching reframe/render.
  Boundaries must stay on the native-frame grid; unit tests do NOT catch a 1-frame content-splice flash —
  Task 4 (frame-step on real video) is MANDATORY.
- **No silent fallbacks (rule #8)** — a non-authoritative plan-read logs the reason and falls back to CV;
  never `except: pass`, never a silent default.
- **Commit gate** — `just check` green before every commit; commit from PowerShell (pre-commit needs `just`
  on the refreshed PATH). Conventional commits. If ruff-format reformats → `git add` + recommit.
- **Types are codegen** — no `models.py` shape change is required in P0 (the plan reuses existing dicts). If
  any `models.py` field is added, run `just types`; never hand-edit `packages/shared`.
- **PATH refresh in EVERY PowerShell call:**
  `$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`
- **Branch:** `feat/editor-reframe-nle`. Local-first — do NOT deploy; stop at green local + Task 4 evidence.

---

## File Structure

- `services/worker/app/editor/reframe_cache.py` — add pure `plan_to_regions(...)`; add `fps` to
  `build_persist_payload(...)`. (One responsibility: plan ⇄ regions serialization/resolution. It already
  owns `regions_from_persisted`/`build_persist_payload`/`_region_from_dict` — this belongs here.)
- `services/worker/app/run.py:240-242` — pass `fps` into `build_persist_payload` (batch fan-out caller).
- `services/worker/app/main.py:2400-2404` — pass `fps` into `build_persist_payload` (preview backfill caller).
- `services/worker/app/tasks.py:217, 286-307` — wire plan-read before the CV call in `render_edit_to_file`.
- `services/worker/tests/unit/test_reframe_cache.py` — unit tests for `plan_to_regions` + payload `fps`.
- `services/worker/tmp/verify_p0_grid.py` (scratch, gitignored) — Task 4 Δ=0 frame-step harness.

---

### Task 1: Store `fps` in the persisted plan payload

**Files:**
- Modify: `services/worker/app/editor/reframe_cache.py:302-315` (`build_persist_payload`)
- Modify: `services/worker/app/run.py:240-242`
- Modify: `services/worker/app/main.py:2400-2404`
- Test: `services/worker/tests/unit/test_reframe_cache.py`

**Interfaces:**
- Produces: `build_persist_payload(regions, default_start, default_end, *, fps: float) -> dict` — output dict
  now contains `"fps": fps` alongside `default_start`, `default_end`, `regions`.

- [ ] **Step 1: Write the failing test** — append to `test_reframe_cache.py`:

```python
def test_build_persist_payload_stores_fps():
    from app.editor.reframe_cache import build_persist_payload
    from app.pipeline.stage3_reframe import TrackPoint, TrackRegion

    regions = [TrackRegion(t0=0.0, t1=2.0, mode="fill",
                           points=(TrackPoint(t=0.0, mode="fill", cx=0.4),))]
    payload = build_persist_payload(regions, 10.0, 20.0, fps=29.97002997)
    assert payload["fps"] == 29.97002997
    assert payload["default_start"] == 10.0
    assert payload["default_end"] == 20.0
    assert payload["regions"][0]["mode"] == "fill"
```

- [ ] **Step 2: Run test to verify it fails**

Run (PowerShell, PATH-refreshed): `cd services/worker; uv run pytest tests/unit/test_reframe_cache.py::test_build_persist_payload_stores_fps -v`
Expected: FAIL — `TypeError: build_persist_payload() got an unexpected keyword argument 'fps'`.

- [ ] **Step 3: Add the `fps` param + field** — replace `build_persist_payload` signature/body (`reframe_cache.py:302-315`):

```python
def build_persist_payload(
    regions: list[TrackRegion], default_start: float, default_end: float, *, fps: float
) -> dict[str, Any]:
    """Регионы дефолтного интервала клипа → durable-payload (домен 1, batch персист). PURE.

    Хранит границы дефолтного интервала (сверка с edit-интервалами) + НАТИВНЫЙ fps, на котором
    границы регионов были frame-snapped (plan-read рендером обязан рендерить на ТОМ ЖЕ fps, иначе
    −1 flash — см. REFRAME_FPS_GRID_INVARIANT / parse_fps-постмортем), + сами регионы (той же
    сериализацией, что acc_*.json / reframe_<clip>.json → один контракт).
    """
    return {
        "default_start": round(default_start, 3),
        "default_end": round(default_end, 3),
        "fps": fps,
        "regions": [_region_to_dict(r) for r in regions],
    }
```

- [ ] **Step 4: Update the two callers.**

`run.py:240-242` — the batch fan-out. Pass the same fps the reframe used in `render_one_clip` (the `fps`
variable already passed to the reframe call in that function; grep upward in the function for the fps bound
before this block):

```python
        db.put_reframe_regions(
            out.name, clip_id, build_persist_payload(regions, seg.start, seg.end, fps=fps)
        )
```

`main.py:2400-2404` — preview backfill. Uses `meta.fps` (already in scope in this endpoint):

```python
            db.put_reframe_regions(
                job_id,
                clip_id,
                build_persist_payload(region_lists[0], iv.source_start, iv.source_end, fps=meta.fps),
            )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd services/worker; uv run pytest tests/unit/test_reframe_cache.py::test_build_persist_payload_stores_fps -v`
Expected: PASS.

- [ ] **Step 6: Run the fps-flash guard + full worker unit slice**

Run: `cd services/worker; uv run pytest tests/unit/test_reframe_cache.py tests/unit/test_reframe_resolve.py -q`
Expected: PASS (no regression on existing persist/resolve tests — they call `build_persist_payload` without
`fps` if any; if a pre-existing test breaks on the new required kwarg, update that call to pass `fps=25.0`).

- [ ] **Step 7: Commit**

```
git add services/worker/app/editor/reframe_cache.py services/worker/app/run.py services/worker/app/main.py services/worker/tests/unit/test_reframe_cache.py
git commit -m "feat(reframe): store native fps in persisted plan payload (P0 grid-safety gate)"
```

---

### Task 2: `plan_to_regions` — persisted plan → render regions, or None

**Files:**
- Modify: `services/worker/app/editor/reframe_cache.py` (add function after `regions_from_persisted`, ~:351)
- Test: `services/worker/tests/unit/test_reframe_cache.py`

**Interfaces:**
- Consumes: payload from `build_persist_payload` (Task 1), `intervals_match_default`,
  `apply_overrides_to_regions`, `_region_from_dict` (all in this module), `SourceInterval`/`CropOverride`.
- Produces: `plan_to_regions(persisted, intervals, overrides, *, fps, fps_tol=1e-6) -> list[list[TrackRegion]] | None`
  — same shape `render_timeline` expects (per-interval, interval-relative `TrackRegion`); `None` = not
  authoritative → caller must CV-recompute.

- [ ] **Step 1: Write the failing tests** — append to `test_reframe_cache.py`:

```python
def _sample_persisted(fps=30.0):
    return {
        "default_start": 10.0, "default_end": 20.0, "fps": fps,
        "regions": [
            {"t0": 0.0, "t1": 4.0, "mode": "fill",
             "points": [{"t": 0.0, "mode": "fill", "cx": 0.4}], "points_b": [], "fit_style": "blur_zoom"},
            {"t0": 4.0, "t1": 10.0, "mode": "fit",
             "points": [], "points_b": [], "fit_style": "blur_zoom"},
        ],
    }

def test_plan_to_regions_authoritative_untrimmed():
    from app.editor.reframe_cache import plan_to_regions
    from app.models import SourceInterval
    regions = plan_to_regions(_sample_persisted(), [SourceInterval(source_start=10.0, source_end=20.0)],
                              [], fps=30.0)
    assert regions is not None and len(regions) == 1
    assert [r.mode for r in regions[0]] == ["fill", "fit"]
    assert regions[0][0].points[0].cx == 0.4
    assert regions[0][0].t0 == 0.0 and regions[0][1].t1 == 10.0

def test_plan_to_regions_none_on_trim():
    from app.editor.reframe_cache import plan_to_regions
    from app.models import SourceInterval
    assert plan_to_regions(_sample_persisted(), [SourceInterval(source_start=12.0, source_end=20.0)],
                           [], fps=30.0) is None

def test_plan_to_regions_none_on_fps_mismatch():
    from app.editor.reframe_cache import plan_to_regions
    from app.models import SourceInterval
    assert plan_to_regions(_sample_persisted(fps=29.97), [SourceInterval(source_start=10.0, source_end=20.0)],
                           [], fps=25.0) is None

def test_plan_to_regions_none_when_legacy_no_fps():
    from app.editor.reframe_cache import plan_to_regions
    from app.models import SourceInterval
    p = _sample_persisted(); del p["fps"]
    assert plan_to_regions(p, [SourceInterval(source_start=10.0, source_end=20.0)], [], fps=30.0) is None

def test_plan_to_regions_applies_override_repaint():
    from app.editor.reframe_cache import plan_to_regions
    from app.models import CropOverride, SourceInterval
    ov = CropOverride(source_start=10.0, source_end=14.0, mode="fit")  # force first shot wide
    regions = plan_to_regions(_sample_persisted(), [SourceInterval(source_start=10.0, source_end=20.0)],
                              [ov], fps=30.0)
    assert regions is not None
    assert regions[0][0].mode == "fit"  # repainted, boundaries preserved
    assert regions[0][0].t0 == 0.0 and regions[0][0].t1 == 4.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd services/worker; uv run pytest tests/unit/test_reframe_cache.py -k plan_to_regions -v`
Expected: FAIL — `ImportError: cannot import name 'plan_to_regions'`.

- [ ] **Step 3: Implement `plan_to_regions`** — add after `regions_from_persisted` in `reframe_cache.py`:

```python
def plan_to_regions(
    persisted: dict[str, Any],
    intervals: list[SourceInterval],
    overrides: list[CropOverride],
    *,
    fps: float,
    fps_tol: float = 1e-6,
) -> list[list[TrackRegion]] | None:
    """Persisted batch plan → render-ready регионы БЕЗ CV, либо None если план НЕ авторитетен. PURE.

    Авторитетен ⇔ (a) план несёт fps, совпадающий с render-fps (иначе baked frame-snapped границы
    построены на ДРУГОЙ сетке → −1 flash, REFRAME_FPS_GRID_INVARIANT / parse_fps-постмортем), И
    (b) edit — ОДИН интервал, равный дефолтному span'у плана (нетронутый трим). Иначе None →
    вызыватель честно пересчитывает CV (правило №8, без тихого фолбэка). Тот же путь
    deserialize+overrides, что у regions_from_persisted → результат идентичен resolve_regions_accurate,
    только без CV. Возвращает форму render_timeline: list[list[TrackRegion]] (per-interval, 0-based).
    """
    plan_fps = persisted.get("fps")
    if plan_fps is None or abs(plan_fps - fps) > fps_tol:
        return None
    ds, de = persisted.get("default_start"), persisted.get("default_end")
    if ds is None or de is None:
        return None
    if not intervals_match_default(intervals, ds, de):
        return None
    iv = intervals[0]
    regions = [_region_from_dict(d) for d in persisted.get("regions", [])]
    regions = apply_overrides_to_regions(regions, overrides, iv, fps=fps)
    return [regions]
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd services/worker; uv run pytest tests/unit/test_reframe_cache.py -k plan_to_regions -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```
git add services/worker/app/editor/reframe_cache.py services/worker/tests/unit/test_reframe_cache.py
git commit -m "feat(reframe): plan_to_regions - persisted plan to render regions without CV (P0)"
```

---

### Task 3: Wire plan-read into `render_edit_to_file` (with explicit CV fallback)

**Files:**
- Modify: `services/worker/app/tasks.py:217` (import) and `:286-307` (the reframe block)

**Interfaces:**
- Consumes: `plan_to_regions` (Task 2), `db.get_reframe_regions(job_id, clip_id) -> dict | None`,
  `edit.lock_per_shot`, `src_fps` (re-probed at `tasks.py:270`), `_rlog`.
- Produces: `regions: list[list[TrackRegion]]` fed to the UNCHANGED `render_timeline(...)` call (`:308-324`).

- [ ] **Step 1: Update the import** (`tasks.py:217`):

```python
    from app.editor.reframe_cache import plan_to_regions, resolve_regions_accurate
```

- [ ] **Step 2: Replace the reframe block** (`tasks.py:284-307`, the comment + `regions = resolve_regions_accurate(...)` call) with the plan-read-first version:

```python
    # ЕДИНЫЙ источник плана: сперва пробуем ПРОЧИТАТЬ запечённый batch-план (без CV) — детерминизм
    # превью==рендер + быстрый ре-рендер. Плейн-рид авторитетен ТОЛЬКО для locked+untrimmed+fps-match
    # (зеркало preview fast-path get_clip_reframe). Иначе — честный CV-пересчёт (правило №8).
    regions: list[list[TrackRegion]] | None = None
    if edit.lock_per_shot:
        persisted = db.get_reframe_regions(job_id, clip_id)
        if persisted:
            regions = plan_to_regions(
                persisted, edit.source_intervals, edit.reframe_overrides, fps=src_fps
            )
    if regions is None:
        _rlog.info(
            "render: plan-read miss (follow/trim/fps-mismatch/no-plan) -> CV recompute job=%s clip=%s",
            job_id, clip_id,
        )
        regions = resolve_regions_accurate(
            out / "source.mp4", edit.source_intervals, edit.reframe_overrides,
            src_w=meta.width, src_h=meta.height, fps=src_fps, clip_id=clip_id,
            out_dir=out, cache_dir=out / "analysis", mode_setting=s.reframe_mode,
            speaker_crop_scale=s.reframe_speaker_crop_scale, face_fps=s.reframe_face_fps,
            smoothing=s.reframe_smoothing, lock_per_shot=edit.lock_per_shot,
            min_hold_sec=s.reframe_min_hold_sec, speak_threshold=s.reframe_speak_threshold,
            scene_threshold=s.reframe_scene_threshold, min_scene_sec=s.reframe_min_scene_sec,
            split_enabled=s.reframe_split_enabled, wide_speak_min=s.reframe_wide_speak_min,
        )
    else:
        _rlog.info("render: plan-read HIT (no CV) job=%s clip=%s", job_id, clip_id)
```

Add the `TrackRegion` import to the function's import block if not present (top of `render_edit_to_file`,
alongside the existing `from app.pipeline.stage5_render import ...`):

```python
    from app.pipeline.stage3_reframe import TrackRegion  # noqa: PLC0415 (torch lazy)
```

(Leave the `render_timeline(...)` call at `:308-324` UNCHANGED — it already consumes `regions`.)

- [ ] **Step 3: Type/lint gate**

Run: `cd services/worker; uv run mypy app/tasks.py app/editor/reframe_cache.py; uv run ruff check app/tasks.py app/editor/reframe_cache.py`
Expected: clean (no type errors; `regions` typed `list[list[TrackRegion]] | None` then narrowed).

- [ ] **Step 4: Run the worker unit suite**

Run: `cd services/worker; uv run pytest tests/unit -q`
Expected: PASS (no regressions).

- [ ] **Step 5: Commit**

```
git add services/worker/app/tasks.py
git commit -m "feat(render): render_edit_to_file reads persisted plan (no CV) on authoritative path (P0)"
```

---

### Task 4: Δ=0 verification on a REAL render (MANDATORY — unit tests can't catch the flash)

**Files:**
- Create: `services/worker/tmp/verify_p0_grid.py` (scratch, gitignored — NOT committed)

**Why:** `REFRAME_FPS_GRID_INVARIANT.md` — `verify_grid_fix.py` Δ=0 does NOT catch a content-splice
misalignment; only frame-stepping the real render at a fill↔fit seam does. The plan-read path must produce
pixels IDENTICAL to the CV path for the same locked+untrimmed clip.

- [ ] **Step 1: Pick a real fixture.** A job with persisted `reframe_regions` and ≥1 fill↔fit seam,
  ideally a ≠25fps source (29.97 exposes rounding). Ask the founder for a `job_id`/`clip_id` (e.g. a Cardi B
  clip), or reuse the local QA harness from [[clip-quality-reframe-night-2026-07-07]]
  (`import_upload → run.run_pipeline`) to produce one. **Clear stale edits first:**
  `DELETE FROM clip_edits WHERE job_id=?` (local `app.db._DB_PATH`) so `ensure_edit` rebuilds current state.

- [ ] **Step 2: Render BOTH paths.** Write `verify_p0_grid.py` to render the SAME clip twice:
  (a) plan-read HIT (default: locked+untrimmed → `render_edit_to_file` takes the plan),
  (b) forced CV (temporarily set `edit.lock_per_shot=False` OR monkeypatch `plan_to_regions` to return
  `None`) → CV path. Save `plan.mp4` and `cv.mp4`.

- [ ] **Step 3: Frame-step at each fill↔fit seam.** For each region boundary `t`, extract the frames at
  `t-1/fps`, `t`, `t+1/fps` from both mp4s with ffmpeg and compare (PSNR / pixel-diff). Expected: **identical
  frames at every seam (Δ=0), no 1-frame old-shot-in-new-mode flash.**

- [ ] **Step 4: Eyeball the seam.** Per the invariant doc — verify by EYE, frame-by-frame, on the real
  render at the content splice. Capture before/after PNGs to the scratchpad. Record the result (job_id,
  seam times, PSNR) in the P0 completion note.

- [ ] **Step 5: Report to founder** — plan-read HIT == CV render (Δ=0) on a real ≠25fps clip. Do NOT deploy;
  this is the local-first green state for the founder to test in-hand ([[local-first-then-deploy]]).

---

## Self-Review

**Spec coverage (§4a render-reads-plan):** Task 2 (`plan_to_regions`) + Task 3 (wiring) implement "render
reads the plan instead of CV"; Task 1 adds the fps-safety the spec's §12 Δ=0 requirement demands; Task 4 is
the spec's mandated frame-step verification. Trim-beyond-plan / follow → explicit CV fallback (spec §4a,
rule #8) = the `regions is None` branch in Task 3. Source-absolute plan + per-shot typed model are OUT of P0
(spec §13 P0 refined: render-read first; typed `FrameShot` lands in P1 where the UI consumes it — YAGNI).

**Placeholder scan:** none — every step has exact code/commands. Task 1 Step 4 `run.py` fps: the executor
greps the in-scope reframe fps (the value already passed to the reframe call in `render_one_clip`); the edit
shape is shown. Task 4 needs a real fixture (job_id) from the founder/local harness — that's inherent to a
frame-step check, not a placeholder.

**Type consistency:** `plan_to_regions(persisted, intervals, overrides, *, fps, fps_tol)` used identically in
Task 2 (def) and Task 3 (call). Return `list[list[TrackRegion]] | None` matches `render_timeline`'s `regions`
param and the `regions` local in Task 3. `build_persist_payload(..., *, fps)` def (Task 1 Step 3) matches both
caller edits (Task 1 Step 4). `db.get_reframe_regions(job_id, clip_id)` matches the confirmed signature
(`db.py:608`).

**Open risk carried into execution:** on a plan-read HIT the whole `source.mp4` is still downloaded
(`ensure_source`, needed to re-probe fps + encode) — P0 saves only the CV, not the download. The download +
prebake win is P4 (defer-render-to-export). This is intended P0 scope.

---

## Execution Handoff

Recommended: **subagent-driven-development** — fresh subagent per task, orchestrator reviews + runs `just
check` + commits between tasks (single git index / gate). Task 4 (real-video Δ=0) the orchestrator runs
directly (needs a real fixture + eyeball), not a Sonnet subagent.
