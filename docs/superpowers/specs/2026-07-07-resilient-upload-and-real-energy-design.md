# Resilient Resumable Upload + Real Clip Energy — Design (2026-07-07)

> Status: **approved for implementation** (founder approved scope + verification via real local R2 E2E).
> Two independent features shipped together this session, plus a memory/limits hardening part.
> Source-of-truth anchors are cited as `file:line` against the repo at design time.

## Problem

1. **Large uploads (≈5 GB) are unstable** — they stall/hang mid-upload or fail, and there is no
   recovery. Root causes (confirmed by code trace):
   - `putPart` (`apps/web/lib/api.ts:214-244`) is a single XHR with **no `timeout`/`ontimeout`** →
     a stalled socket on a flaky network fires neither `onload` nor `onerror`; because
     `uploadParts` awaits `Promise.all` (`api.ts:302`), one stuck part **freezes the whole upload
     at ~99% forever**, with no error and no abort.
   - **No retry/backoff** — one part returning non-2xx / null-ETag throws and aborts the entire
     ~320-part multipart upload (`api.ts:293-294`, catch at `api.ts:361-370`).
   - **All ~320 presigned URLs are minted at t0 with a fixed 6h TTL** (`storage.py:284`,
     `main.py:458-461`) and **never renewed** → slow uplinks (<~2 Mbps for 5 GB) hit expiry → 403 →
     whole-upload abort.
   - **No resume** — any failure aborts the multipart upload and the user re-picks + re-uploads from
     0% (`dashboard/page.tsx:268-279,396`; `SourceForm.tsx:48` drops the `File`).
   - **Stalls are invisible** — progress just freezes; the `ReconnectBanner` is for status polling,
     not the upload phase.

2. **The "energy" signal bar is always at minimum on every clip.** `computeSignalBars`
   (`apps/web/components/ClipCard.tsx:62-68`) derives energy purely from `clip.tone` and requires an
   exact match against 7 lowercase words; the select prompt asks for **free-form** tone
   ("one or two words", open list incl. `infuriating`) — so tone rarely matches → energy collapses to
   the `:1` fallback (clamped to ≥1). The **same** `clip.tone` also silently breaks the emoji badge
   (`ClipCard.tsx:218`) and the colored card glow (`ClipCard.tsx:217,242`). Meanwhile the pipeline
   already computes **real audio energy** (`preview_moments.py:extract_loudness/detect_energy_moments`)
   that is never surfaced per clip.

3. **Memory / OOM ceiling** — the upload itself is memory-safe (blob slices, ≤6 parts in flight), but
   the downstream pipeline downloads the whole 5–10 GB source to **tmpfs=RAM** (`deploy.ps1:71,77` —
   `TMPDIR=/root/data/tmp`), so a large source + decoded frames can OOM-kill the Job. Already flagged
   at "2h/1440p" in the deploy script; a 22-clip job was SIGKILL'd previously.

## Goals

- A large upload survives flaky networks: per-part timeout, stall watchdog, retry-with-backoff,
  presigned-URL renewal, and **resume** from already-uploaded parts (no restart-from-0%).
- Honest, visible UX: "reconnecting / retrying part X" during transient failures; a terminal error
  only after retries are exhausted, with a button that **actually resumes**.
- The energy bar reflects **real per-clip audio loudness**; emoji/glow work (tone vocabulary aligned).
- A clear, documented **operational envelope** (length / size / time / memory) and a concrete answer
  to "will it OOM?", with the low-risk hardening applied now.

## Non-goals

- Rewriting the render pipeline to stream/range-fetch the source (the proper OOM fix, `Part C3`) is
  **designed and recommended here but gated on a separate decision** — it touches `stage3/stage5`
  render which is under the reframe frame-grid invariant and deserves its own cycle.
- Changing pricing/plans, the select algorithm, or reframe logic.

---

## Part A — Resilient resumable upload

### A.1 Server (worker)

**`services/worker/app/storage.py`**
- `list_parts(job_id, upload_id) -> list[dict]` — wrap R2 `ListParts`; return
  `[{"PartNumber": n, "ETag": e, "Size": s}, ...]` sorted by PartNumber. `JobError` on failure
  (rule #8). Handle "no such upload" as a clear error, not silent empty.
- Reuse existing `presigned_upload_part_url` for per-part URL (re)issue.

**`services/worker/app/main.py`** — new endpoint:
- `POST /jobs/{job_id}/upload-parts`, body `UploadPartsBody{ upload_id: str, part_numbers: list[int] | None }`
  → `{ part_size, completed: [{part_number, etag}], parts: [{part_number, url}] }`.
  - `completed` = from `storage.list_parts` (authoritative "already in R2").
  - `parts` = fresh presigned URLs for `part_numbers` (or for all not-yet-completed parts when
    `part_numbers` is omitted — the resume case). `part_size` = `storage.plan_part_size(size?)`; since
    the client already knows `part_size` from the original plan, echo it back for reconciliation.
  - Auth: same posture as `upload-complete` (valid token + unguessable `job_id`; the DB job row does
    not exist until `upload-complete`, so resume lives on R2 via `ListParts`). Quota is **not**
    re-charged here (no job spawned).
- Also add a **server-side size cap** (Part C1) on `UploadUrlBody.size` (see Part C).

**Contract (`services/worker/app/models.py`, orchestrator-owned):** add the request/response models
for `upload-parts` if we choose to type them in `models.py`; otherwise local Pydantic models in
`main.py` (they are not part of the shared TS contract the frontend generates from — the frontend
calls this endpoint with hand-written types, like the other upload endpoints). **Decision:** keep
`upload-parts` request/response as local `main.py` Pydantic models (consistent with
`UploadUrlBody`/`UploadCompleteBody`), so no `packages/shared` codegen churn for the upload path.

### A.2 Client (frontend)

**New pure module `apps/web/lib/uploadResilience.ts`** (unit-tested, no browser):
- `fileFingerprint(file: {name; size; lastModified}) -> string`
- `remainingParts(allPartNumbers: number[], completed: Set<number>) -> number[]`
- `backoffDelay(attempt: number) -> number` — exponential + jitter, capped.
- `classifyPartError(status: number | null, kind: 'timeout'|'stall'|'network'|'http') ->
  'retry' | 'refresh' | 'fatal'` — 403/expired → `refresh`; timeout/stall/network/5xx → `retry`;
  other 4xx → `fatal`.

**New `apps/web/lib/uploadCheckpoint.ts`** (IndexedDB):
- `loadCheckpoint(fingerprint) -> { job_id, upload_id, part_size, completed: {part_number,etag}[] } | null`
- `saveCheckpoint(fingerprint, cp)` / `updatePart(fingerprint, {part_number,etag})` / `clearCheckpoint(fingerprint)`
- Best-effort: any IndexedDB failure degrades to "no resume" (upload still works), never throws into
  the upload path.

**`apps/web/lib/api.ts`** — rework the multipart branch of `createUploadJob` + `uploadParts` + `putPart`:
- `putPart`: add `xhr.timeout` (hard per-request ceiling) + `xhr.ontimeout`, and a **stall watchdog**
  — if `xhr.upload.onprogress` reports no new bytes for `STALL_MS`, abort the XHR and reject with a
  `stall` kind. Both become retryable via `classifyPartError`.
- Wrap each part in a **retry loop** (max `PART_MAX_ATTEMPTS`, e.g. 5) using `backoffDelay`; on
  `refresh`, call `POST /jobs/{id}/upload-parts` for `[n]` to get a fresh URL, then retry; on `fatal`
  or exhausted attempts, throw.
- **Resume:** at the start of the multipart branch, compute `fingerprint`; if a checkpoint exists,
  call `upload-parts` (no `part_numbers`) to get server `completed` + fresh URLs for the rest;
  reconcile `completed = union(checkpoint, serverListParts)`; upload only `remainingParts`. Persist
  each completed part's ETag to the checkpoint as it lands. On full success, `clearCheckpoint`.
- **Blast radius:** a shared per-upload `AbortController` so a fatal part stops the sibling 6.
- **Progress state:** extend the progress callback payload to carry
  `phase: 'uploading' | 'retrying' | 'stalled'` + `{ part, attempt }` so the UI can narrate.

**`apps/web/app/(app)/dashboard/page.tsx` + `apps/web/components/SourceForm.tsx`:**
- Keep the `File` reference on error (do not discard on the error screen) so "resume" works without
  re-picking a 5 GB file.
- Render the new states: a non-fatal "Reconnecting… retrying part X (attempt k)" banner during
  retries; escalate to `ErrorPanel` only after a part is exhausted, with a primary button that
  **resumes** (re-enters the multipart branch → checkpoint short-circuits to the remaining parts).
  Label reflects reality ("Продолжить загрузку" / "Resume upload"); keep a separate "Start over".

**i18n:** add the new strings to **both** `apps/web/messages/en.json` + `ru.json` (idiomatic RU),
called via `t()` (CLAUDE.md rule — never hardcode user-facing text).

### A.3 Error handling
- Every terminal failure surfaces a clear message + is logged; no silent fallbacks (rule #8).
- Watchdog/timeout/5xx/network → retry with backoff. 403/expired → refresh URL → retry. Non-retryable
  4xx or exhausted attempts → fatal → abort siblings → error panel + resume affordance (checkpoint
  retained so a retry resumes).

---

## Part B — Real per-clip energy

### B.1 Contract (orchestrator-owned)
- `services/worker/app/models.py`: add `energy: float | None = None` (0..1) to `Segment` and
  `ClipOut`. Free-string `tone` stays as-is. Run `just types` → regenerates `packages/shared` (never
  hand-edit).

### B.2 Compute (worker, pure + TDD)
- New pure function (in `pipeline`, near `preview_moments.py`): `clip_energy(loudness, start, end) ->
  float | None`. Uses the same loudness curve already produced by `extract_loudness`
  (`preview_moments.py:129-158`) and threaded into select (`run.py` → `select_segments(loudness=…)`).
  Normalize the clip window's mean/peak RMS to 0..1 **relative to the video's own loudness
  distribution** (mirror `detect_energy_moments` intensity `(v-med)/(hi-med)`, `preview_moments.py:116`),
  clamped to [0,1]. Returns `None` when no loudness curve is available (e.g. Groq path without RMS).
- Populate in `run.build_clip_out` (`run.py:108-125`): `energy=clip_energy(loudness, seg.start, seg.end)`.
- Preserve `energy` through the B1 vision re-rank `model_copy` (`stage2b_vision_review.py:299,305`) —
  update only touches score, so `energy` carries automatically (verify).

### B.3 Frontend
- `apps/web/components/ClipCard.tsx` `computeSignalBars`: the `energy` branch reads `clip.energy`
  (0..1 → 1..4 bars) when present; falls back to the existing tone heuristic only when
  `clip.energy == null` (back-compat for pre-existing clips). Add `energy` to the `clipEqual`
  memo comparator (`ClipCard.tsx:137-155`).

### B.4 Tone vocabulary (fixes emoji + glow, same root)
- Constrain the select prompt (`prompts/select_moments.v2.txt:37-38` and the fallback copy at
  `stage2_select.py:624-625`) to emit exactly one token from the UI's closed set
  (`shocking/funny/controversial/inspiring/relatable/insightful/touching`).
- Add a **server-side normalize/whitelist** in postprocess (`stage2_select.py:442-444`) mapping
  common synonyms (e.g. `infuriating`→`controversial`, `moving`/`emotional`→`touching`) to the
  canonical set and coercing unknowns to a documented default. Never trust the LLM to stay in-vocab.
- Keep `tone` a free string on the model (no enum, no codegen change); the whitelist lives in worker
  postprocess.

---

## Part C — Memory / OOM ceiling + operational envelope

### C1 — Server-side size cap (small, included now)
- `UploadUrlBody.size` (`main.py:419`) currently has no upper bound (client-only 10 GB guard, which is
  bypassable). Add a server cap = the honest supported ceiling and return 413/400 with a clear message
  above it. Value chosen to stay within the memory envelope (see report). Defense-in-depth + defines
  the real "weight" limit.

### C2 — Config truth (small, included now)
- Reconcile `deploy/cloudrun/deploy.ps1` with the **live** band-aid (render memory 16→32Gi if that is
  the live value) so the repo isn't stale, and document the envelope inline. No prod deploy from here
  (user-gated).

### C3 — Lift the ceiling (designed, recommended, decision-gated)
- **Option C3a (config):** add a disk-backed ephemeral volume to the Jobs via
  `gcloud beta run jobs update … --add-volume`/`--add-volume-mount` so `TMPDIR` is disk, not RAM.
  Removes the "source in RAM" pressure. Deploy is user-gated (auto-mode blocks prod gcloud).
- **Option C3b (code, proper):** in `quip-render`, **range-fetch only the clip's byte window** from R2
  instead of `download_source` (whole file). Cuts render RAM to per-clip working set. Bigger, touches
  the render read path (reframe-invariant care) → separate cycle. **Recommended as immediate
  follow-up.**

### C4 — Limits report (explicit deliverable)
After implementation, report to the founder: max video **length**, max **file size**, **time**
budgets (upload + processing), and the **memory/OOM** story with numbers and the residual ceiling.

---

## Verification (local first, then trust prod)

1. **`just check` green** — the commit gate: ruff + mypy + tsc + eslint + unit tests + anti-drift.
   - Vitest (frontend): `uploadResilience` pure helpers; retry/stall/refresh/resume via a mock XHR
     (stall→watchdog→retry→ok; 5xx→backoff→ok; 403→refresh→ok; exhaust→fatal; resume skips completed);
     `ClipCard` energy mapping (0..1→bars) + null fallback.
   - Pytest (worker): `upload-parts` endpoint (mocked `ListParts` → completed + fresh URLs; auth);
     `clip_energy` pure (silent→low, loud→high, empty curve→None, window math); tone normalize/whitelist.
2. **Local real-R2 E2E** (founder-approved): set `storage_backend=r2` + R2 creds in the local worker
   `.env`, run worker + `next dev`; upload a multi-GB synthetic file; toggle **DevTools → Offline**
   mid-upload to force a stall/drop → observe retry UI → resume dials the tail → object assembled in R2
   → job proceeds. Same bucket/CORS/presign as prod = faithful proxy. (R2 CORS already allows
   `http://localhost:3000`, `storage.py:335`.)
3. **Energy locally** — unit tests + one real small local job → energy bars vary and track loudness.
4. **Cloud** — after deploy: one real large upload on prod + verify energy on a fresh clip.

## Rollout & agent boundaries

1. **Orchestrator** first lands the contract: `models.py` (`energy`) → `just types`
   (regenerates `packages/shared`). Owns all shared files (`models.py`, `packages/shared`).
2. **Parallel, strict file boundaries:**
   - **Backend agent:** `storage.py`, `main.py`, `stage2_select.py` (tone), `run.py` (energy),
     the `clip_energy` pure fn + worker tests.
   - **Frontend agent:** `lib/api.ts`, `lib/uploadResilience.ts`, `lib/uploadCheckpoint.ts`,
     `dashboard/page.tsx`, `SourceForm.tsx`, `ClipCard.tsx`, `messages/{en,ru}.json` + vitest.
3. **Orchestrator:** `just check`, integration, local E2E, commit (PowerShell, cyrillic → `-F` file),
   then deploy on founder's go. Update `docs/README.md` "Current reality" + `docs/JOURNAL.md`.

## Open decisions
- **C3 (lift OOM ceiling): now or fast-follow?** Recommend fast-follow (C3b touches render).
- Exact numeric size cap (C1) — set from the memory envelope in the report.
