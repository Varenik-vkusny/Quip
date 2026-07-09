# Autonomous scaling session — 2026-07-10 (night)

> **LIVE RUNBOOK + ROLLBACK LOG.** Founder granted full autonomy overnight to test + implement scaling
> optimizations on PROD (`quip-project-500711`, europe-west1) and record everything. This file is the
> single source of truth for **what changed, how to roll it back, and the benchmarks after each step.**
> If this session dies, ANYONE can read this file to understand state + revert.

**Rules honored:** no quality loss (verify), don't break prod (verify each change + rollback ready),
benchmark after EACH optimization, record rollback for every prod change.

**Test video:** `C:\Users\user\Downloads\Telegram Desktop\CARDI B_ The TRUTH Behind The End of My Relationship….mp4`

---

## 🔴 ROLLBACK QUICK REFERENCE (every prod change + how to undo)

> PATH refresh first in every PowerShell call:
> `$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`
> `$env:CLOUDSDK_CORE_DISABLE_PROMPTS="1"`

| # | Change | Applied? | Rollback command | Verified safe? |
|---|--------|----------|------------------|----------------|
| Q1 | Quota: Total Ephemeral Disk 100GiB→2TiB (europe-west1) | requested, reconciling (Google review) | `gcloud quota preferences` delete `quip-disk-ew1` (raising a ceiling is harmless; leave it) | n/a (ceiling) |
| Q2 | Quota: Total memory 400GiB→2TiB | ✅ GRANTED | harmless ceiling, leave | n/a |
| Q3 | Quota: Instances 100→300 | requested, reconciling | harmless ceiling, leave | n/a |
| — | (pending changes recorded below as executed) | — | — | — |

**Baseline prod config (to restore if needed):**
- `quip-pipeline`: image `sha256:13db1ee4…`, cpu=8, mem=32Gi, ephemeral-disk=32Gi, task-timeout=10800s, RENDER_FANOUT_MAX=12, RENDER_FANOUT_TIMEOUT=3600
- `quip-render`: image `sha256:13db1ee4…`, cpu=8, mem=32Gi, ephemeral-disk=32Gi, task-timeout=1200s
- `quip-api`: image `sha256:13db1ee4…`, cpu=1, mem=4Gi, concurrency=80, maxScale=20

Restore a job's image: `gcloud run jobs update <job> --region=europe-west1 --image=europe-west1-docker.pkg.dev/quip-project-500711/quip/quip@sha256:13db1ee4944d364986409a426d3a6e9d21de2607fb799679e0c0b52a801cbbbc`
Restore disk: `gcloud beta run jobs update <job> --region=europe-west1 --add-volume=name=tmp,type=ephemeral-disk,size=32Gi --add-volume-mount=volume=tmp,mount-path=/root/data/tmp`
Restore coord cpu: `gcloud run jobs update quip-pipeline --region=europe-west1 --cpu=8`

---

## 📊 BENCHMARKS (after each optimization)

| Step | Config | Job wall time | Peak parallel render | Per-clip median | Quality check | Notes |
|------|--------|---------------|----------------------|-----------------|---------------|-------|
| BASELINE | 32GiB disk, 3-parallel cap | (measured pre-session: 30-clip job ~19min, 2-3 parallel) | 2-3 | ~50s compute | — | from 2026-07-09 log analysis |
| (rows appended as tests run) | | | | | | |

---

## 📓 CHRONOLOGICAL LOG (what I did, what happened)

### Setup
- Created this runbook.
- Probed test video: **86.5-min Cardi B interview, 720p h264, 532 MB** (up to 30 clips → heavy fan-out test).
- Confirmed `.env` has R2 + Supabase service-role + Deepgram/Gemini creds → can trigger a real cloud job manually (create job row in Supabase + upload source to R2 + `gcloud run jobs execute quip-pipeline JOB_KIND=upload_job`).
- Trigger path: `cloudrun_job.py` JOB_KIND `upload_job` → `run_upload_job_from_r2(job_id, filename, max_clips, user_id, ...)`.
- Quota re-check (t≈45min): disk still 100 GiB (pending), mem 2048 GiB (granted), instances pending.

### Plan for the night (in priority order)
1. **WS1 — Cloud parallelism benchmark (founder's #1):** baseline 32GiB job on Cardi B → measure wall/concurrency → apply disk-reduce 32→16 (reversible) → re-run → A/B. Verify clips render (quality inherently preserved — disk-size change doesn't touch render code). ⚠️ Gated on: trigger recipe + whether the auto-mode classifier permits prod `gcloud` mutations under the founder's blanket overnight authorization.
2. **WS2 — Content dedup (SAFE, no reframe risk):** per-user skip-reupload via fingerprint. Pure code + TDD + local test.
3. **WS3 — Big-video segmentation (RISKY, touches reframe grid):** design + LOCAL frame-exact prototype + prove Δ=0/byte-identical. ⚠️ Do NOT deploy to prod without visual frame-by-frame proof (founder's hard "no quality loss").

⚠️ **Classifier risk:** earlier prod `gcloud ... update` was auto-blocked. Founder has since given explicit blanket overnight authorization, but the harness classifier may still gate individual prod mutations. If blocked, WS1's prod steps get documented with exact commands + expected results for the founder to run; WS2/WS3 code work proceeds regardless.

### 🚧 CONFIRMED BLOCKER (t≈50min): prod mutations are classifier-blocked
Tested `gcloud beta run jobs update quip-render` (apply disk=16 then rollback to 32, net-zero) → **DENIED by the auto-mode classifier**, twice, despite the founder's verbal blanket authorization. The harness gate is independent of in-conversation permission. **I cannot autonomously: change job config (disk/cpu), deploy new images, or (almost certainly) trigger cloud jobs.** To unblock future sessions, founder can add a Bash permission rule in `.claude/settings.json` (per the denial message) for `gcloud run jobs update`/`services update`/`builds submit`/`run jobs execute`, or run the prepared commands manually.

### 🔄 REVISED PLAN (code-first, prod steps prepared for founder to apply)
The disk-reduce hack (WS1) was always a temporary bridge; the REAL fix is **segmentation (WS3)** which makes render containers tiny regardless of source size → solves parallelism AND big videos properly, and is provable locally (Δ=0). So the pivot is strictly better:
1. **WS3 — segment extraction (PRIMARY):** implement + LOCAL frame-exact proof (Δ=0 / identical clips). Benchmark locally (render time; quality-identical proof). Prepare deploy + `reframe_regions` cache-clear steps for founder.
2. **WS2 — content dedup:** implement backend core + TDD (local).
3. **Big-video config bumps** (MAX_UPLOAD, MAX_VIDEO_MINUTES, coordinator task-timeout) — edit in code/deploy.ps1, ready to apply.
4. **WS1 (parallelism benchmark):** documented for founder to run — exact commands + expected 2-3→~5 (16GiB) / →12 (post grant) parallel.
All code gated through `just check`; nothing deployed by me (can't). Everything left in the working tree + this runbook.

### Prep done (t≈55min)
- Read + internalized `docs/REFRAME_FPS_GRID_INVARIANT.md` fully. **WS3 safety criterion:** a stream-copy (`-c copy`) segment keeps frames byte-identical; `segment_frame = source_frame − K` (K = keyframe frame, integer) → `round(t·fps)` shifts by an integer → grid Δ=0 preserved IFF K is an exact frame boundary AND the reframe offset math is re-anchored to the segment origin. **Non-negotiable gate:** render the same clip via the CURRENT full-source path and the NEW segment path locally, prove FRAME-IDENTICAL (PSNR=∞ / byte-identical PNG dumps) on a real ≠25fps video before shipping. If not provably identical → do not ship.
- Spawn-retry (dispatch.py) already implemented + TDD earlier this session (ruff+mypy+1366 unit green). deploy.ps1 drift fixed. Not yet committed (will commit to a feature branch at a checkpoint).
- 3 design agents in flight: cloud-job trigger recipe (for documenting WS1 commands), dedup blueprint (WS2), segmentation blueprint (WS3).
