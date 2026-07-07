# Resilient Upload + Real Energy — Implementation Plan

> **For agentic workers:** implement task-by-task, TDD. Steps use checkbox (`- [ ]`) syntax.
> Spec: `docs/superpowers/specs/2026-07-07-resilient-upload-and-real-energy-design.md`.

**Goal:** Make large uploads survive flaky networks (timeout + stall-watchdog + retry/backoff +
presigned-URL renewal + resume from uploaded parts, with honest retry UX), and make the clip "energy"
bar reflect real per-clip audio loudness (fixing the emoji/glow tone bug too). Plus a memory/limits
hardening pass.

**Architecture:** Direct browser→R2 multipart stays; we add client resilience (pure helpers + IndexedDB
checkpoint + retry loop) and a server `upload-parts` endpoint (ListParts + fresh presigned URLs) for
resume/renewal. Energy = a pure `clip_energy()` over the existing loudness curve, added to the `ClipOut`
contract via codegen, rendered by `ClipCard`.

**Tech Stack:** Next.js (modified — read `apps/web/AGENTS.md`), TypeScript, Vitest, XHR/IndexedDB;
FastAPI + boto3 (R2/S3 multipart), Pydantic, pytest; type codegen `models.py → just types`.

## Global Constraints (verbatim from CLAUDE.md — apply to EVERY task)

- **Commit gate:** `just check` (ruff + mypy + tsc + eslint + unit tests + anti-drift) must be GREEN
  before every commit. Commit ONLY from PowerShell (pre-commit hook needs `just` on refreshed PATH).
  Cyrillic commit message → write to a UTF-8 file and `git commit -F <file>`.
- **TDD on pure logic** (part math, checkpoint reconciliation, backoff, error classification,
  `clip_energy` normalization, tone whitelist): failing test first, then code.
- **Types = codegen only:** change `services/worker/app/models.py`, then `just types`. NEVER hand-edit
  `packages/shared/*`.
- **No silent fallbacks:** errors surface (JobError / failed status / explicit thrown error + log).
- **i18n:** every new user-facing string goes into BOTH `apps/web/messages/en.json` and `ru.json`
  (idiomatic RU, no mixed RU/EN), called via `t()`.
- **PowerShell PATH refresh** before any `just`/`uv`/`ffmpeg`:
  `$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`
- **apps/web is a modified Next.js** — read `apps/web/AGENTS.md` before web code.
- **Reframe invariant untouched** — this plan does not modify `stage3_reframe`/`stage5_render`.

---

## File Structure

**Orchestrator (contract, shared):**
- Modify `services/worker/app/models.py` — add `energy: float | None` to `Segment` + `ClipOut`.
- Regenerated (do not hand-edit): `packages/shared/src/types.ts`, `contract.json`.

**Backend workstream (agent BACKEND — strict boundary):**
- Modify `services/worker/app/storage.py` — add `list_parts()`.
- Modify `services/worker/app/main.py` — add `POST /jobs/{job_id}/upload-parts`; add size cap in
  `create_upload_url`.
- Create `services/worker/app/pipeline/clip_energy.py` (or add to `preview_moments.py`) — pure
  `clip_energy()`.
- Modify `services/worker/app/run.py` — set `energy=` in `build_clip_out`.
- Modify `services/worker/app/pipeline/stage2_select.py` + `prompts/select_moments.v2.txt` — tone
  whitelist/normalize + constrained prompt.
- Tests: `services/worker/tests/unit/test_multipart.py`, `test_upload_api.py`, new
  `test_clip_energy.py`, `test_tone_normalize.py`.

**Frontend workstream (agent FRONTEND — strict boundary):**
- Create `apps/web/lib/uploadResilience.ts` (pure) + `apps/web/lib/uploadResilience.test.ts`.
- Create `apps/web/lib/uploadCheckpoint.ts` (IndexedDB).
- Modify `apps/web/lib/api.ts` — resilient multipart path (`putPart`, `uploadParts`, `createUploadJob`).
- Modify `apps/web/app/(app)/dashboard/page.tsx` + `apps/web/components/SourceForm.tsx` — retry UX +
  keep File on error + resume.
- Modify `apps/web/components/ClipCard.tsx` — energy bar from `clip.energy` + memo comparator.
- Modify `apps/web/messages/en.json` + `apps/web/messages/ru.json` — new strings.
- Tests: `apps/web/lib/uploadResilience.test.ts`, `apps/web/components/ClipCard.test.tsx` (energy map).

**Infra (orchestrator, Part C — code only, deploy user-gated):**
- Modify `deploy/cloudrun/deploy.ps1` — reconcile render memory to live value; document envelope.

---

## PHASE 0 — Contract (orchestrator, BEFORE dispatching agents)

### Task 0: Add `energy` to the contract

**Files:** Modify `services/worker/app/models.py` (`Segment` ~L92, `ClipOut` ~L141).
**Produces:** `Segment.energy: float | None`, `ClipOut.energy: float | None` + regenerated TS
`ClipOut.energy?: number | null`.

- [ ] Add `energy: float | None = None` to `Segment` and `ClipOut` with a docstring
      ("0..1 normalized clip loudness; None when no RMS curve").
- [ ] Run `just types`; confirm `packages/shared/src/types.ts` gains `energy?: number | null` on both.
- [ ] `just check` green; commit `feat(models): add per-clip energy field to contract`.

---

## PHASE 1 — Backend workstream (agent BACKEND)

### Task B1: `storage.list_parts()`

**Files:** Modify `services/worker/app/storage.py`; Test `tests/unit/test_multipart.py`.
**Produces:** `list_parts(job_id: str, upload_id: str) -> list[dict]` →
`[{"part_number": int, "etag": str, "size": int}, ...]` sorted by part_number.

- [ ] **Failing test** (`test_multipart.py`): monkeypatch `_r2_client` to return a fake `list_parts`
      response with unordered parts; assert `storage.list_parts(...)` returns them normalized + sorted;
      assert a client exception → `JobError`.
- [ ] Run: `uv run pytest tests/unit/test_multipart.py -v` → FAIL.
- [ ] Implement `list_parts` wrapping `_r2_client().list_parts(Bucket, Key=source_object_key(job_id),
      UploadId=upload_id)`, paginating `Parts`, normalizing keys, `JobError` on failure (rule #8).
- [ ] Run test → PASS. Commit `feat(storage): list_parts for resumable multipart`.

### Task B2: `POST /jobs/{job_id}/upload-parts` endpoint

**Files:** Modify `services/worker/app/main.py`; Test `tests/unit/test_upload_api.py`.
**Consumes:** `storage.list_parts`, `storage.presigned_upload_part_url`, `storage.plan_part_size`.
**Produces:** endpoint returning `{part_size, completed:[{part_number,etag}], parts:[{part_number,url}]}`.

- [ ] **Failing test**: with mocked storage, POST `{upload_id, part_numbers:[2,3]}` → response has
      `completed` from `list_parts` and `parts` = fresh URLs for [2,3]; POST with `part_numbers` omitted
      → `parts` = URLs for all not-yet-completed part numbers (derive count from a passed `size` or from
      the max known part). Auth: no token → 401.
- [ ] Run → FAIL.
- [ ] Add `UploadPartsBody(BaseModel){ upload_id: str; part_numbers: list[int] | None = None;
      size: int | None = None }` and the handler. Resolve `_resolve_user` (auth) like `upload-complete`.
      `completed = storage.list_parts(job_id, upload_id)`; `done = {p["part_number"] for p in completed}`;
      target = `part_numbers` or `[n for n in 1..plan_part_count(size) if n not in done]`;
      `parts=[{"part_number":n,"url":storage.presigned_upload_part_url(job_id,upload_id,n)} for n in target]`.
- [ ] Run → PASS. Commit `feat(api): upload-parts endpoint (resume + URL renewal)`.

### Task B3: Server-side size cap (Part C1)

**Files:** Modify `services/worker/app/main.py` (`UploadUrlBody`/`create_upload_url` ~L419-468);
Test `tests/unit/test_upload_api.py`.
**Produces:** 413 (or 400) with clear detail when `size` exceeds `MAX_UPLOAD_BYTES`.

- [ ] **Failing test**: POST `/jobs/upload-url` with `size` above cap → 413 + detail mentioning the GB
      limit; at/below cap → normal plan.
- [ ] Run → FAIL.
- [ ] Add `MAX_UPLOAD_BYTES` (from envelope, e.g. `10 * 1024**3`), raise `HTTPException(413, detail=...)`
      in `create_upload_url` before minting. Keep client guard in sync (`SourceForm.MAX_UPLOAD_MB`).
- [ ] Run → PASS. Commit `feat(api): server-side upload size cap (defense-in-depth)`.

### Task B4: `clip_energy()` pure function

**Files:** Create `services/worker/app/pipeline/clip_energy.py`; Test `tests/unit/test_clip_energy.py`.
**Consumes:** the loudness curve shape produced by `preview_moments.extract_loudness`
(list of `(t_seconds, db)` or the module's actual structure — match it exactly).
**Produces:** `clip_energy(loudness, start: float, end: float) -> float | None`.

- [ ] Inspect `preview_moments.py:extract_loudness`/`detect_energy_moments` for the exact curve type.
- [ ] **Failing test** (`test_clip_energy.py`): a synthetic loudness curve; a window over a LOUD region
      → energy near 1.0; a window over a QUIET region → near 0.0; empty/None curve → None; window with
      no samples → None; result always within [0,1].
- [ ] Run → FAIL.
- [ ] Implement: gather samples in `[start,end]`; compute the window mean (or 90th-percentile) RMS;
      normalize relative to the whole curve's distribution as `(v - med) / (hi - med)` (mirror
      `detect_energy_moments`, `preview_moments.py:116`), clamp to `[0,1]`; return None when no curve or
      no in-window samples. PURE (no IO). Docstring per rule.
- [ ] Run → PASS. Commit `feat(pipeline): clip_energy from loudness curve`.

### Task B5: Populate `energy` in `build_clip_out`

**Files:** Modify `services/worker/app/run.py` (`build_clip_out` ~L108-125).
**Consumes:** `clip_energy`, the `loudness` already threaded into `run.py`.
**Produces:** clips carry real `energy`.

- [ ] Confirm `loudness` is in scope where `build_clip_out` is called (it is passed to
      `select_segments`); thread it to `build_clip_out` if needed.
- [ ] Set `energy=clip_energy(loudness, seg.start, seg.end)` in the `ClipOut(...)` construction.
- [ ] Verify `stage2b_vision_review.apply_review` `model_copy(update={...})` preserves `energy`
      (only score changes) — add/extend a test asserting `energy` survives the re-rank.
- [ ] `just check` green; commit `feat(run): surface real per-clip energy`.

### Task B6: Tone vocabulary whitelist + prompt constraint

**Files:** Modify `services/worker/app/pipeline/stage2_select.py` (postprocess ~L442-444, fallback vocab
~L624-625); `prompts/select_moments.v2.txt` (~L37-38); Test `tests/unit/test_tone_normalize.py`.
**Produces:** `normalize_tone(raw: str | None) -> str | None` mapping to the UI's 7-word set.

- [ ] **Failing test**: `normalize_tone("infuriating")=="controversial"`; `"deeply moving"=="touching"`;
      `"funny"=="funny"`; `"FUNNY "=="funny"`; unknown → documented default (e.g. `"insightful"` or
      `None` — pick one, assert it); `None`→`None`.
- [ ] Run → FAIL.
- [ ] Implement `normalize_tone` (canonical set + synonym map), call it in postprocess where `tone` is
      read (`stage2_select.py:442-444`). Constrain the prompt copy to emit exactly one token from the set
      (drop "one or two words"/trailing "…"/`infuriating`).
- [ ] Run → PASS. Commit `fix(select): normalize tone to UI vocabulary (energy/emoji/glow)`.

---

## PHASE 2 — Frontend workstream (agent FRONTEND) — runs in parallel with Phase 1

### Task F1: `uploadResilience.ts` pure helpers

**Files:** Create `apps/web/lib/uploadResilience.ts` + `apps/web/lib/uploadResilience.test.ts`.
**Produces:**
- `fileFingerprint(f:{name:string;size:number;lastModified:number}):string`
- `remainingParts(all:number[], completed:Set<number>):number[]`
- `backoffDelay(attempt:number):number`  // exp + jitter, capped
- `classifyPartError(status:number|null, kind:'timeout'|'stall'|'network'|'http'):'retry'|'refresh'|'fatal'`

- [ ] **Failing tests** (`uploadResilience.test.ts`, Vitest):
      `fileFingerprint` stable + distinct on size change; `remainingParts([1,2,3,4], {1,3})→[2,4]`;
      `backoffDelay(0)<backoffDelay(3)` and `backoffDelay(n)<=CAP`; `classifyPartError(403,'http')==='refresh'`,
      `(null,'stall')==='retry'`, `(500,'http')==='retry'`, `(400,'http')==='fatal'`.
- [ ] Run: `pnpm -C apps/web vitest run lib/uploadResilience.test.ts` → FAIL.
- [ ] Implement the four pure functions (deterministic core; jitter via a passed `rand=Math.random`
      param so tests can seed it).
- [ ] Run → PASS. Commit `feat(upload): pure resilience helpers`.

### Task F2: `uploadCheckpoint.ts` (IndexedDB)

**Files:** Create `apps/web/lib/uploadCheckpoint.ts`.
**Produces:** `loadCheckpoint(fp)`, `saveCheckpoint(fp,cp)`, `updatePart(fp,{part_number,etag})`,
`clearCheckpoint(fp)` where `cp = {job_id, upload_id, part_size, completed:{part_number,etag}[]}`.

- [ ] Implement a tiny IndexedDB wrapper (one store, key=fingerprint). All ops best-effort: on any
      IndexedDB error, resolve to null/no-op (never throw into the upload path). No test required (thin IO
      shim); exercised by the E2E.
- [ ] Commit `feat(upload): IndexedDB checkpoint store`.

### Task F3: Resilient multipart in `api.ts`

**Files:** Modify `apps/web/lib/api.ts` (`putPart` L214-244, `uploadParts` L255-306, `createUploadJob`
multipart branch L343-371).
**Consumes:** F1 helpers, F2 checkpoint, backend `POST /jobs/{id}/upload-parts`.
**Produces:** resilient upload with timeout/watchdog/retry/refresh/resume + progress phases.

- [ ] `putPart`: set `xhr.timeout = PART_TIMEOUT_MS` + `xhr.ontimeout` (reject kind `timeout`); add a
      stall watchdog (`setInterval`/timer resetting on `onprogress`; no bytes for `STALL_MS` → `xhr.abort()`
      + reject kind `stall`). Clear timers on settle.
- [ ] Wrap each part in a retry loop using `backoffDelay`/`classifyPartError`; on `refresh` call
      `upload-parts` for `[n]` to get a fresh URL then retry; persist ETag to checkpoint on success;
      report `phase:'retrying'|{part,attempt}` / `'stalled'` via the progress callback.
- [ ] `createUploadJob` multipart branch: compute `fingerprint`; if checkpoint exists, call `upload-parts`
      (omit part_numbers) → reconcile `completed = union(checkpoint, serverListParts)` → upload only
      `remainingParts`; on full success `clearCheckpoint`; keep a shared `AbortController` so a fatal part
      stops siblings.
- [ ] Extend the `UploadProgressInfo` type with `phase` + optional `{part, attempt}`.
- [ ] `just check` (tsc/eslint) green; commit `feat(upload): timeout+watchdog+retry+resume in client`.

### Task F4: Retry UX + keep File + resume button

**Files:** Modify `apps/web/app/(app)/dashboard/page.tsx` (UploadProgress L91-155, handleSubmitFile
L226-254, handleReset L268-279, ErrorPanel wiring L395-397); `apps/web/components/SourceForm.tsx`
(File state L48); `apps/web/messages/{en,ru}.json`.
**Consumes:** F3 progress `phase`.

- [ ] Render the `retrying`/`stalled` phase as a non-fatal banner in `UploadProgress`
      ("Reconnecting… retrying part {n} (attempt {k})"). Add i18n keys to BOTH catalogs.
- [ ] On error, retain the `File` (don't clear it in the flow that leads to ErrorPanel) so resume can
      re-enter `createUploadJob` and short-circuit via checkpoint.
- [ ] ErrorPanel primary action = **Resume** (re-run upload → checkpoint resumes); keep a secondary
      "Start over" = full reset. Add i18n keys to BOTH catalogs.
- [ ] `just check` green; commit `feat(upload): honest retry/resume UX`.

### Task F5: Energy bar from `clip.energy`

**Files:** Modify `apps/web/components/ClipCard.tsx` (`computeSignalBars` L62-68, `clipEqual` L137-155);
Test `apps/web/components/ClipCard.test.tsx`.
**Consumes:** contract `ClipOut.energy` (Phase 0).

- [ ] **Failing test**: `computeSignalBars({...clip, energy:0.95}).find(s=>s.key==='energy').bars===4`;
      `energy:0.1 → 1`; `energy:0.5 → ~2-3`; `energy:null` falls back to tone-based (existing behavior).
- [ ] Run: `pnpm -C apps/web vitest run components/ClipCard.test.tsx` → FAIL.
- [ ] Energy branch: `if (clip.energy != null) bars = 1 + Math.round(clip.energy * 3)` (→1..4); else the
      existing tone heuristic. Add `energy` to `clipEqual`.
- [ ] Run → PASS; commit `feat(clipcard): energy bar from real loudness`.

---

## PHASE 3 — Integration & verification (orchestrator)

### Task V1: `just check` full green
- [ ] PATH refresh; `just check` → all green (ruff, mypy, tsc, eslint, pytest, vitest, anti-drift).

### Task V2: Local real-R2 E2E (founder-approved)
- [ ] Set `storage_backend=r2` + R2 creds in local worker env; run worker + `next dev`.
- [ ] Upload a multi-GB synthetic file; DevTools → Offline mid-upload → confirm retry banner → back
      online → resume dials remaining parts → object assembled → job proceeds. Capture the console/network
      evidence.
- [ ] Reload the page mid-upload → "Resume" continues from checkpoint (not from 0%).

### Task V3: Energy locally
- [ ] Run one real small local job; confirm energy bars vary and track loudness (deterministic dbg,
      not clip number).

### Task V4: Infra truth (Part C2) + docs
- [ ] Reconcile `deploy/cloudrun/deploy.ps1` render memory to the live value; comment the envelope.
- [ ] Update `docs/README.md` "Current reality" (upload resilience + energy) and add `docs/JOURNAL.md`
      lines.

### Task V5: Limits report to founder
- [ ] Produce the envelope: max length, max size, time budgets, memory/OOM story + residual ceiling +
      C3 recommendation.

---

## Self-Review (done)

- **Spec coverage:** A (B1–B3,F1–F4) ✓, B (0,B4–B6,F5) ✓, C1 (B3) ✓, C2 (V4) ✓, C3 (documented,
  decision-gated) ✓, C4 report (V5) ✓, verification (V1–V3) ✓.
- **Placeholders:** size cap value + tone default are explicit decisions flagged in-task, not vague.
- **Type consistency:** `energy: float|None`↔`number|null`; `phase` field added in F3 consumed in F4;
  `upload-parts` response shape consistent across B2/F3.
