# Billing Clarity + Studio Tier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. The orchestrator (Opus) dispatches ONE subagent per task, reviews between tasks, integrates, runs `just check`, and commits. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is tagged **[Sonnet]** (atomic/mechanical, strict file bounds) or **[Opus]** (billing correctness, data migration, cross-file wiring).

**Goal:** Make Quip's minute-pool quota honest and clear in the UI, bind the video library to the account (not the device), protect margin against tiny-video abuse, make pay-as-you-go a divisible 60-minute balance, and add a $49 Studio tier — all shipped additively without disrupting live users.

**Architecture:** The billing engine is already minute-pooled and correct (`billing.py` is the source of truth; `plans.ts` mirrors it for display). Every change here is additive: new UI copy, one new server endpoint, one new billing constant, one new DB column, and one new plan enum value. No existing rows are rewritten; no wire contract (`packages/shared`) changes.

**Tech Stack:** Python worker (FastAPI on Modal), Next.js web (Vercel, **customized fork** — read `node_modules/next/dist/docs/` before frontend code), Supabase Postgres, Polar billing (live), `next-intl` cookie-mode i18n.

## Global Constraints

- **TDD is mandatory on all pure billing logic** (quota, floor, PAYG split): failing test first, then code. (CLAUDE.md rule.)
- **`just check` must be green before every commit** (ruff + mypy + tsc + eslint + unit + anti-drift). Commit only from **PowerShell** (pre-commit hook calls `just`). PATH refresh at the top of every PowerShell call: `$env:PATH = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`.
- **Per-file `git add` only — NEVER `git add -A`.** The working tree has unrelated in-progress changes (social/, editor, ExportMenu). Stage exactly the files each task lists.
- **Types are codegen-only** from `models.py` (`just types`). Do NOT hand-edit `packages/shared/*`. (This plan touches no wire types.)
- **No silent fallbacks** (`except: pass`, silent default): error → `JobError` / failed status / explicit log. (CLAUDE.md rule.)
- **i18n: never hardcode user-facing text.** Every new string goes into BOTH `apps/web/messages/en.json` AND `ru.json` (or both `en`/`ru` blocks of `landingContent.ts`), Russian must be idiomatic, no mixed RU/EN in one string.
- **`billing.py` ⇄ `plans.ts` must stay in sync** — the anti-drift check in `just check` enforces the plan numbers match.
- **Conventional commits.** Cyrillic commit messages → write to a UTF-8 (no BOM) file + `git commit -F <file>` (piping mangles encoding).
- **Worker deploy:** after worker code changes, `$env:PYTHONIOENCODING="utf-8"; modal deploy deploy/modal/worker.py`. Deploy hits shared prod — coordinate timing (see Phase 4 ordering).
- **Docs sync:** behaviour changes update `docs/README.md` reality section + a line in `docs/JOURNAL.md` in the same pass.

## Phase order & live-safety

Ship phases **in order**, each as its own commit(s) behind green `just check`. Phases 1–3 are frontend/worker-code only (Vercel/Modal redeploy, no DB dependency ordering). Phase 4 has a **hard migration-first ordering** (see its safety note). PAYG-divisible (Phase 3b) has a short migrate-then-deploy transition window.

- **Phase 1 — Account-scoped library** (device→email). Safest, immediate value, zero migration.
- **Phase 2 — Minutes-primary UI + stale-copy fixes.** Copy/design only.
- **Phase 3a — Min-billed-minutes floor** (margin guard). One billing constant + `check_quota`, TDD.
- **Phase 3b — Divisible PAYG minutes.** New column + backfill + billing wiring, TDD.
- **Phase 4 — Studio $49 / 2520 min tier.** Migration-first, then worker, then Polar product, then web.

Deferred / out of scope (documented, NOT executed here unless the founder asks): aligning quota reset to the Polar billing anniversary (currently calendar-1st — a real margin leak, but a larger change); raising `MAX_VIDEO_MINUTES` for Studio; the coarse create-time gate letting a near-empty user push bytes to R2 before by-length rejection.

---

# Phase 1 — Account-scoped library (device → email)

**Problem (from audit):** job/clip rows are correctly stored under the authenticated `user_id` (`jobs.user_id`, RLS, `idx_jobs_user`), but the dashboard library reads from browser `localStorage` (`lib/recent.ts`), and there is **no server "list my jobs" endpoint**. Same email on a new device → empty list. Fix is purely additive: add a user-scoped list endpoint and hydrate the library from it; localStorage becomes an optimistic cache.

## File structure

- `services/worker/app/cloud_state.py` — add `list_jobs_for_user(user_id)` (PostgREST query, mirrors `get_job_row`).
- `services/worker/app/db.py` — add dual-mode `list_jobs_for_user(user_id)` wrapper (mirrors `get_job`).
- `services/worker/app/main.py` — add auth-gated `GET /jobs`; harden `GET /jobs/{job_id}` with an owner check.
- `apps/web/lib/api.ts` — add `listJobs()` (mirrors `getUsage`, sends `authHeaders()`).
- `apps/web/components/app/RecentProjects.tsx` — hydrate from the server list; keep localStorage as optimistic cache.
- Tests: `services/worker/tests/unit/test_editor_api.py` (or the existing jobs-endpoint test file) for the new endpoint.

## Task 1.1: Backend `list_jobs_for_user` (cloud + dual-mode) **[Sonnet]**

**Files:**
- Modify: `services/worker/app/cloud_state.py` (add function next to `get_job_row`, ~line 278)
- Modify: `services/worker/app/db.py` (add wrapper next to `get_job`, ~line 447)
- Test: `services/worker/tests/unit/test_db.py` (or nearest db-layer test; create if none)

**Interfaces:**
- Produces: `db.list_jobs_for_user(user_id: str) -> list[dict]` — newest-first list of `{id, status, source_minutes, created_at}` for that user. Cloud: PostgREST `GET /jobs?user_id=eq.<uid>&select=id,status,source_minutes,created_at&order=created_at.desc&limit=50`. Dual-mode SQLite: `SELECT id,status,source_minutes,created_at FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 50`.

- [ ] **Step 1: Read the patterns to mirror.** Read `cloud_state.py` `get_job_row` (~278-286) and `db.py` `get_job` (~447-458) to copy their exact PostgREST/SQLite dual-mode style and error handling (no silent fallback).
- [ ] **Step 2: Write the failing test.**

```python
# test_db.py
def test_list_jobs_for_user_returns_newest_first(tmp_path, monkeypatch):
    # dual-mode SQLite: insert two jobs for user A, one for user B
    from app import db
    db.insert_job("job_a1", "upload", "a1.mp4", user_id="user-A")
    db.insert_job("job_a2", "upload", "a2.mp4", user_id="user-A")
    db.insert_job("job_b1", "upload", "b1.mp4", user_id="user-B")
    rows = db.list_jobs_for_user("user-A")
    ids = [r["id"] for r in rows]
    assert ids == ["job_a2", "job_a1"]  # newest first, user-B excluded
```

- [ ] **Step 3: Run test to verify it fails.** PowerShell (PATH refresh first): `cd services/worker; uv run pytest tests/unit/test_db.py::test_list_jobs_for_user_returns_newest_first -v` → FAIL (`list_jobs_for_user` not defined). *(Match the test-runner invocation already used by the repo — check `justfile`.)*
- [ ] **Step 4: Implement `cloud_state.list_jobs_for_user`** mirroring `get_job_row` (same client, headers, `raise_for_status`), selecting the columns above ordered `created_at.desc` with `limit=50`.
- [ ] **Step 5: Implement `db.list_jobs_for_user`** dual-mode wrapper mirroring `get_job` (cloud path → `cloud_state.list_jobs_for_user`; local path → the SQLite SELECT above).
- [ ] **Step 6: Run test to verify it passes.** Same command → PASS.
- [ ] **Step 7: Commit.** `git add services/worker/app/cloud_state.py services/worker/app/db.py services/worker/tests/unit/test_db.py` then `git commit -m "feat(jobs): add list_jobs_for_user (cloud + dual-mode)"`.

## Task 1.2: `GET /jobs` endpoint (auth-scoped) + harden `GET /jobs/{job_id}` **[Opus]**

**Files:**
- Modify: `services/worker/app/main.py` (add `GET /jobs` near `/usage` ~line 207; add owner check to `get_job` at 527-533)
- Test: `services/worker/tests/unit/test_editor_api.py`

**Interfaces:**
- Consumes: `_resolve_user(authorization, x_user_id)` (main.py:123), `db.list_jobs_for_user` (Task 1.1), `db.get_job` + `db.get_job_owner`/job row `user_id`.
- Produces: `GET /jobs` → `{"jobs": [...]}` for the authenticated user (401 if auth on and no user); `GET /jobs/{job_id}` returns 403/404 when the caller is authenticated and not the owner (legacy `user_id IS NULL` rows stay readable — never reassign them).

- [ ] **Step 1: Read the owner-check pattern** used by `apply-style-all` (main.py ~1505-1514) to reuse the exact "authenticated + owner mismatch → 403, null-owner tolerated" logic.
- [ ] **Step 2: Write the failing test** — `GET /jobs` with a valid user returns only that user's jobs; without auth (auth enabled) returns 401.
- [ ] **Step 3: Run test to verify it fails.**
- [ ] **Step 4: Implement `GET /jobs`** mirroring `/usage`'s header params + `_resolve_user`. If auth enabled and `user_id is None` → 401. Return `{"jobs": db.list_jobs_for_user(user_id)}`. The worker uses the service-role key (bypasses RLS) so the endpoint MUST filter by `user_id` itself — never rely on RLS.
- [ ] **Step 5: Harden `get_job`** — accept `authorization`/`x_user_id` headers; if auth enabled and the job's `user_id` is non-null and ≠ caller → 403; null-owner (legacy) stays readable. Keep 404 when missing.
- [ ] **Step 6: Run tests to verify they pass.**
- [ ] **Step 7: Commit.** `git add services/worker/app/main.py services/worker/tests/unit/test_editor_api.py` → `git commit -m "feat(jobs): user-scoped GET /jobs + owner check on GET /jobs/{id}"`.

## Task 1.3: Frontend `listJobs()` API client **[Sonnet]**

**Files:**
- Modify: `apps/web/lib/api.ts` (add `listJobs` mirroring `getUsage` ~317-321)

**Interfaces:**
- Produces: `export async function listJobs(): Promise<{ jobs: Array<{ id: string; status: JobStatusLite; source_minutes: number; created_at: string }> }>` — GETs `/jobs` with `authHeaders()`.

- [ ] **Step 1: Read `getUsage` (api.ts ~317-321) and `authHeaders` (~22-31)** to mirror exact fetch/error style.
- [ ] **Step 2: Implement `listJobs()`** GETting `${API}/jobs` with `await authHeaders()`; on non-ok, throw the same shape `getUsage` throws (no silent fallback).
- [ ] **Step 3: Verify tsc.** PowerShell: `just tsc` (or the web tsc task) → no errors.
- [ ] **Step 4: Commit.** `git add apps/web/lib/api.ts` → `git commit -m "feat(api): add listJobs() client for user-scoped job list"`.

## Task 1.4: Hydrate library from server; localStorage becomes optimistic cache **[Opus]**

**Files:**
- Modify: `apps/web/components/app/RecentProjects.tsx` (list source at ~88)
- Modify (light): `apps/web/lib/recent.ts` (no schema change; it stays as the optimistic cache — `addRecentProject` calls at `dashboard/page.tsx:141,163` remain as instant inserts)

**Design:** On mount (authenticated), call `listJobs()` and merge the server list with the local `getRecentSnapshot()` — server is the source of truth for *existence/ownership*; local cache supplies instant optimistic entries for a job the user just created before the server list refreshes, and the cached `reviewed`/`label` fields. De-dupe by `id`, newest-first. On a fresh device the server list populates the library; on the creating device the optimistic entry shows immediately and reconciles.

- [ ] **Step 1: Read `RecentProjects.tsx`** fully + `apps/web/AGENTS.md` (customized Next.js) + the relevant `node_modules/next/dist/docs/` guide for data fetching in this fork.
- [ ] **Step 2: Add server hydration** — fetch `listJobs()` (via the app's existing client-fetch/SWR pattern — match how `useUsage`/`UsageMeter` fetches), map rows → `RecentProject` shape, merge with the local snapshot (de-dupe by `id`, prefer server `status`, keep local `reviewed`/`label`), render the merged list. Preserve the existing empty-state and per-job status poller.
- [ ] **Step 3: Verify tsc + eslint** (`just tsc`, lint) → clean.
- [ ] **Step 4: Manual smoke (orchestrator)** — note for verification: log into the same account from a second browser profile and confirm prior clips now appear (per the live-editor Playwright memory). This is a display-only change; no data is written.
- [ ] **Step 5: Commit.** `git add apps/web/components/app/RecentProjects.tsx apps/web/lib/recent.ts` → `git commit -m "feat(library): hydrate recent projects from user-scoped server list"`.

## Task 1.5: Ship + verify Phase 1 **[Opus orchestrator]**

- [ ] Run full `just check` → green.
- [ ] Deploy worker (`modal deploy`) then let Vercel autodeploy on push to `main` (or the feature branch → merge). Verify `GET /jobs` returns the caller's jobs on prod with a real JWT; verify cross-device visibility.
- [ ] Update `docs/README.md` reality section (library now account-scoped) + `docs/JOURNAL.md` line.

---

# Phase 2 — Minutes-primary UI + stale-copy fixes

**Problem (from audit):** the same minute pool is shown as both "N videos" and "N min", with the **video count as the visual hero everywhere** ("10 videos / month", "Videos left", a discrete "0 / 10 used" meter) and the minutes truth in faint secondary text; nothing tells the user a short video costs a *fraction*. Plus three stale/contradictory strings.

## Task 2.1: Fix stale prices + length strings **[Sonnet]**

**Files (exact, from audit):**
- Modify: `apps/web/app/(marketing)/pricing/page.tsx:12-13` — `metadata.description` "$10/mo … $25/mo … $2 a video" → **"$15/mo … $35/mo … $3 a video"**.
- Modify: `apps/web/lib/faq.ts:21` — PAYG "$2 each" → **"$3 each"**; and `faq.ts:9` "90 minutes" → **"3 hours"**.
- Modify: `apps/web/lib/landingContent.ts:273,422` (+ RU `563,712`) — "90-minute"/"90 мин" per-video length → **"3-hour" / "3 часа"** (and the `faq` "90-minute upload counts as 1.5" example → "a 30-minute upload counts as 0.5" to avoid the length clash).
- Modify: `apps/web/components/marketing/HowItWorks.tsx:12` — "90 minutes" → **"3 hours"**.

- [ ] **Step 1:** Grep for the stale literals to catch any extra occurrence: `Grep "\\$10/|\\$25|\\$2 a video|\\$2 each|90 minute|90 мин|90-minute"` under `apps/web`.
- [ ] **Step 2:** Apply the exact replacements above. Russian strings must stay idiomatic. Do NOT change the actual `PAYG_PRICE_USD`/plan numbers (those are already correct).
- [ ] **Step 3:** Verify tsc + eslint clean.
- [ ] **Step 4: Commit.** `git add apps/web/app/(marketing)/pricing/page.tsx apps/web/lib/faq.ts apps/web/lib/landingContent.ts apps/web/components/marketing/HowItWorks.tsx` → `git commit -m "fix(pricing-copy): correct stale $10/$25/$2 prices and 90min→3h length"`.

## Task 2.2: Reframe pricing cards + landing to minutes-primary **[Sonnet]**

**Files:**
- Modify: `apps/web/lib/plans.ts` — restructure each `PlanDisplay` so **minutes lead**. Change `limit` from `"10 videos / month"` to `"600 minutes / month"` (Free "120 minutes / month", Pro "1,800 minutes / month"); change `limitNote` to a positive, count-clarifying line, e.g. Starter: **"≈10 hour-long videos — or many more if they're shorter. A 6-min clip uses just 6 minutes."** (Free/Pro analogous). Update the file header comment to note minutes-primary.
- Modify: `apps/web/components/marketing/PricingCards.tsx:60` — eyebrow "Monthly limit" stays; ensure the large headline renders `limit` (now minutes).
- Modify: `apps/web/lib/landingContent.ts` allowance strings (`360/376/392` + RU `650/666/682`) → lead with minutes: **"600 min / mo · ≈10 videos"** (EN) / **"600 мин / мес · ≈10 видео"** (RU), etc.

- [ ] **Step 1:** Read `plans.ts` + `PricingCards.tsx` + `landingContent.ts` pricing block.
- [ ] **Step 2:** Apply the minutes-first `limit`/`limitNote`/allowance strings above for free/starter/pro (EN+RU). Keep numbers synced with `billing.py` (600/1800/120).
- [ ] **Step 3:** Verify tsc + eslint + the `billing.py`⇄`plans.ts` anti-drift check (`just check` subset) clean.
- [ ] **Step 4: Commit.** `git add apps/web/lib/plans.ts apps/web/components/marketing/PricingCards.tsx apps/web/lib/landingContent.ts` → `git commit -m "feat(pricing-ui): lead with minutes, frame videos as a friendly estimate"`.

## Task 2.3: Dashboard usage meter → minutes-primary + "short videos cost less" cue **[Opus]**

**Files:**
- Modify: `apps/web/components/app/UsageMeter.tsx` — make **"Minutes left"** the hero Stat (currently "Videos left" at ~89); demote videos to the secondary derived line ("≈8.7 videos"). Replace the discrete **"{used} / 10 used"** meter (~103) with a **continuous minutes meter** (`used_minutes / monthly_minutes`). Keep PAYG line.
- Modify: `apps/web/components/app/UsagePill.tsx:52` — lead with **"{minutes} min · ≈{videos} videos"**; update the tooltip (~40) to "Minutes left — a short video uses only its length; 1 video ≈ 60 min."
- Modify: `apps/web/messages/en.json` + `ru.json` (`usageMeter.*` ~91-106, `usagePill` if keyed) — swap the hero label to `minutesLeft`, add a `usageMeter.shortHint` = EN "Short videos use fewer minutes — make 10 long ones or 100 short ones." / RU "Короткие видео тратят меньше минут — сделай 10 длинных или 100 коротких." Add any new keys to BOTH catalogs.

**Interfaces:** consumes the existing `/usage` payload (`remaining_minutes`, `monthly_minutes`, `remaining_videos`, `payg_minutes`) — no backend change needed; all fields already exist (`main.py:_usage_payload`).

- [ ] **Step 1:** Read `UsageMeter.tsx`, `UsagePill.tsx`, and the `usageMeter` blocks in both message catalogs + `apps/web/AGENTS.md`.
- [ ] **Step 2:** Add the new i18n keys to `en.json` AND `ru.json` (idiomatic RU).
- [ ] **Step 3:** Make minutes the hero Stat; convert the meter to a continuous minutes bar; show videos as "≈N videos" secondary; add the `shortHint` line under the meter.
- [ ] **Step 4:** Verify tsc + eslint + i18n key presence (missing key breaks tsc/runtime).
- [ ] **Step 5: Commit.** `git add apps/web/components/app/UsageMeter.tsx apps/web/components/app/UsagePill.tsx apps/web/messages/en.json apps/web/messages/ru.json` → `git commit -m "feat(usage-ui): minutes-primary meter + short-video hint (EN+RU)"`.

## Task 2.4: Ship + verify Phase 2 **[Opus orchestrator]**

- [ ] `just check` green; Vercel autodeploy. Eyeball `/pricing`, dashboard, header pill in EN + RU. Update `docs/JOURNAL.md`.

---

# Phase 3a — Min-billed-minutes floor (margin guard)

**Problem (from economics audit):** the quota captures only Deepgram-linear cost (~$0.0043/min) and misses per-job fixed cost (cold starts, per-clip render/re-download, Gemini floors), so a short video costs 2–5× more per input-minute. 600×1-min uploads in a 600-min Starter month ≈ $18–20 COGS on a $15 plan. Fix: bill each video for **at least** a floor of minutes. Honest videos (>floor) are unaffected.

## File structure
- `services/worker/app/billing.py` — add `MIN_BILLED_MINUTES_PER_VIDEO` constant; apply the floor inside `check_quota`'s pool step.
- `services/worker/tests/unit/test_billing.py` — new floor tests.

## Task 3a.1: Add the floor to `check_quota` (TDD) **[Opus]**

**Files:**
- Modify: `services/worker/app/billing.py` (constant near `MINUTES_PER_VIDEO` ~29; edit `check_quota` step (2) ~229-241)
- Test: `services/worker/tests/unit/test_billing.py`

**Interfaces:**
- Produces: `MIN_BILLED_MINUTES_PER_VIDEO = 2.0`. `check_quota` unchanged signature; consumption split (`from_monthly_min` + `from_payg_min`) now reflects `billed = max(source_minutes, MIN_BILLED_MINUTES_PER_VIDEO)`. `QuotaDecision.minutes` stays = actual `source_minutes`. `_meter` needs NO change (it reads the decision split).

- [ ] **Step 1: Write the failing tests.**

```python
# test_billing.py
def test_tiny_video_billed_at_floor():
    d = billing.check_quota("starter", used_minutes=0.0, payg_minutes=0.0, source_minutes=1.0)
    assert d.allowed
    assert d.from_monthly_min == 2.0  # floored, not 1.0

def test_normal_video_billed_at_real_length():
    d = billing.check_quota("starter", used_minutes=0.0, payg_minutes=0.0, source_minutes=30.0)
    assert d.from_monthly_min == 30.0  # above floor → unchanged

def test_floor_bounds_the_tiny_video_exploit():
    # 600-min Starter pool; each 1-min video bills 2 → at most 300 fit, not 600
    d_at_limit = billing.check_quota("starter", used_minutes=600.0, payg_minutes=0.0, source_minutes=1.0)
    assert not d_at_limit.allowed
```

- [ ] **Step 2: Run tests to verify they fail.** PowerShell + PATH refresh: `cd services/worker; uv run pytest tests/unit/test_billing.py -k floor_or_tiny -v` → FAIL.
- [ ] **Step 3: Implement.** Add the constant with a docstring explaining the per-job-fixed-cost rationale. In `check_quota` step (2), replace `from_monthly = min(source_minutes, monthly_remaining)` / `shortfall = source_minutes - from_monthly` with:

```python
    # Пол списания за видео: фикс. косты на джобу (cold start, рендер клипов, Gemini)
    # НЕ масштабируются с длиной → короткое видео стоит нам непропорционально дорого.
    # Списываем не меньше пола (честные видео > пола не задеты; убивает абьюз «пачка секундных»).
    billed = max(source_minutes, MIN_BILLED_MINUTES_PER_VIDEO)
    monthly_remaining = max(0.0, plan_monthly_minutes(plan) - used_minutes)
    from_monthly = min(billed, monthly_remaining)
    shortfall = billed - from_monthly
```

Keep the `minutes=source_minutes` field and the technical-ceiling check (step 0) on the **actual** `source_minutes`.

- [ ] **Step 4: Run tests to verify they pass.** Also run the FULL `test_billing.py` to catch any existing test asserting the old un-floored split → update those to the floored expectation if they used sub-floor lengths.
- [ ] **Step 5: Commit.** `git add services/worker/app/billing.py services/worker/tests/unit/test_billing.py` → `git commit -m "feat(billing): min-billed-minutes floor per video (anti tiny-video abuse)"`.

## Task 3a.2: Ship Phase 3a **[Opus orchestrator]**

- [ ] `just check` green → `modal deploy`. Update `billing.py` economics docstring is already inline; add a `docs/JOURNAL.md` line. No UI change (floor is invisible to honest users; optionally mention "1-min minimum per video" in the upload-form `creditNote` — defer unless asked).

---

# Phase 3b — Divisible PAYG minutes

**Problem (from audit):** PAYG is stored as whole integer `profiles.payg_credits`, converted `×60` at every read, and consumption rounds UP to whole credits (`payg_credits_for_split` = `ceil(from_payg_min/60)`). A 10-min PAYG video burns a full 60-min credit — 50 minutes wasted. Fix: store a divisible `payg_minutes` balance; $3 grants 60 divisible minutes; consume exact minutes.

**Live-data transition:** apply the migration (adds column + backfill), then **immediately** deploy the worker switched to `payg_minutes`. The window between migration and deploy is seconds–minutes; a PAYG order arriving in that window would credit the old integer column. Mitigation baked in: for the first release the worker reads effective PAYG = `payg_minutes` and the webhook writes `payg_minutes`; a follow-up cleanup migration (not this plan) can zero `payg_credits`. If the founder prefers zero risk, pause PAYG checkout during the swap.

## File structure
- `services/worker/migrations/0016_payg_minutes.sql` — add `profiles.payg_minutes numeric not null default 0`; backfill.
- `services/worker/app/billing.py` — replace `payg_credits_for_split` with `payg_minutes_for_split(decision) -> float` (returns `from_payg_min`, no rounding).
- `services/worker/app/supa.py` + `db.py` — add `add_payg_minutes(user_id, minutes)` and `deduct_payg_minutes(user_id, minutes)`; add `get_profile` `payg_minutes`.
- `services/worker/app/tasks.py` — `_quota_gate` reads `payg_minutes` directly; `_meter` deducts exact minutes.
- `services/worker/app/main.py` — `_enforce_quota`, `_usage_payload`, webhook PAYG branch → minutes.
- `apps/web/components/app/UsageMeter.tsx` + messages — PAYG shown as minutes.
- Tests: `test_billing.py`, `test_polar.py` (PAYG order → minutes), db tests.

## Task 3b.1: Migration — add + backfill `payg_minutes` **[Sonnet]**

**Files:** Create `services/worker/migrations/0016_payg_minutes.sql`.

- [ ] **Step 1: Write the migration** (idempotent, mirrors `0002` style):

```sql
-- 0016_payg_minutes.sql — PAYG становится делимым минутным балансом (было: целые кредиты).
-- Применить (Supabase SQL Editor / db push). Идемпотентна. ПОСЛЕ — сразу задеплоить воркер.
alter table public.profiles
  add column if not exists payg_minutes numeric not null default 0;
-- Разовый бэкфилл: старые целые кредиты → минуты (1 кредит = 60 мин). Гард payg_minutes=0
-- защищает от повторного прогона (после деплоя новый код пишет payg_minutes напрямую).
update public.profiles
  set payg_minutes = payg_credits * 60
  where payg_minutes = 0 and payg_credits > 0;
```

- [ ] **Step 2: Commit.** `git add services/worker/migrations/0016_payg_minutes.sql` → `git commit -m "feat(billing): migration 0016 — divisible payg_minutes column + backfill"`.

## Task 3b.2: DB layer — `add_payg_minutes` / `deduct_payg_minutes` / read `payg_minutes` **[Opus]**

**Files:** Modify `services/worker/app/supa.py` (mirror `add_payg_credits` ~142-164 / `deduct_payg` ~167-185), `services/worker/app/db.py` (dual-mode wrappers), and `get_profile` to include `payg_minutes`. Test: `test_db.py`.

**Interfaces:**
- Produces: `db.add_payg_minutes(user_id: str, minutes: float) -> None` (upsert `payg_minutes += minutes`); `db.deduct_payg_minutes(user_id: str, minutes: float) -> None` (`payg_minutes = MAX(0, payg_minutes - minutes)`); `db.get_profile(user_id)["payg_minutes"]` present (float; dual-mode SQLite adds the column too).

- [ ] **Step 1: Write failing tests** — add 90 minutes then deduct 10 → 80 remain; deduct below 0 floors at 0.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the two functions mirroring the existing credit ones (same idempotency/error style), plus SQLite column + `get_profile` field. Keep the old `add_payg_credits`/`deduct_payg` for now (dormant; removed in a later cleanup).
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit.** `git add services/worker/app/supa.py services/worker/app/db.py services/worker/tests/unit/test_db.py` → `git commit -m "feat(billing): payg_minutes add/deduct/read at db layer"`.

## Task 3b.3: Billing split → exact minutes (TDD) **[Opus]**

**Files:** Modify `services/worker/app/billing.py` (add `payg_minutes_for_split`; leave `payg_credits_for_split` for back-compat or delete if unused after Task 3b.4). Test: `test_billing.py`.

**Interfaces:** Produces `payg_minutes_for_split(decision: QuotaDecision) -> float` → `decision.from_payg_min` if allowed else `0.0` (NO `ceil`).

- [ ] **Step 1: Write failing test** — `decision.from_payg_min == 10.0` → `payg_minutes_for_split == 10.0` (was `ceil→60` under old credits).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `payg_minutes_for_split`.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit.** `git add services/worker/app/billing.py services/worker/tests/unit/test_billing.py` → `git commit -m "feat(billing): payg_minutes_for_split (exact minutes, no round-up)"`.

## Task 3b.4: Wire worker to minutes (gate, meter, enforce, usage, webhook) **[Opus]**

**Files:** Modify `services/worker/app/tasks.py` (`_quota_gate` ~45 → `payg_minutes = float(profile["payg_minutes"])`; `_meter` ~80,88-89 → `payg_min = billing.payg_minutes_for_split(decision)` then `db.deduct_payg_minutes(user_id, payg_min)` guarded by `recorded`), `services/worker/app/main.py` (`_enforce_quota` ~196 → read `payg_minutes`; `_usage_payload` ~225-242 → take `payg_minutes` and derive `payg_videos = minutes_to_videos`; webhook PAYG branch ~514-522 → `db.add_payg_minutes(user_id, credits * billing.MINUTES_PER_VIDEO)`), `services/worker/app/polar.py` (optional: keep `parse_payg_order` returning credits; convert to minutes at the webhook — simplest, one site).

- [ ] **Step 1:** Update `_quota_gate` and `_enforce_quota` to read `profile["payg_minutes"]` directly (drop the `* MINUTES_PER_VIDEO`).
- [ ] **Step 2:** Update `_meter` to deduct exact minutes via `payg_minutes_for_split` + `deduct_payg_minutes` (keep the `recorded` idempotency guard so a re-run doesn't double-deduct).
- [ ] **Step 3:** Update `_usage_payload` + `/usage` to source PAYG from `payg_minutes` (report both `payg_minutes` and `payg_videos = minutes_to_videos(payg_minutes)`).
- [ ] **Step 4:** Update the webhook PAYG branch to `add_payg_minutes(user_id, credits * MINUTES_PER_VIDEO)`.
- [ ] **Step 5:** Run FULL `test_billing.py` + `test_polar.py` + any usage/webhook tests; fix expectations to minutes. Run `just check`.
- [ ] **Step 6: Commit.** `git add services/worker/app/tasks.py services/worker/app/main.py services/worker/app/polar.py services/worker/tests/unit/test_billing.py services/worker/tests/unit/test_polar.py` → `git commit -m "feat(billing): consume/grant PAYG as divisible minutes end-to-end"`.

## Task 3b.5: PAYG UI + copy → "60 minutes, divisible" **[Sonnet]**

**Files:** Modify `apps/web/lib/plans.ts` `PAYG` (~76-82) body → EN "Buy a 60-minute top-up — use it across as many videos as you like. Never expires."; `apps/web/components/app/UsageMeter.tsx` PAYG line → show `payg_minutes` ("N min · never expires"); `messages/en.json`+`ru.json` `usageMeter.purchased` + any PAYG strings (EN+RU idiomatic); landing `landingContent.ts` PAYG (`404-408`/RU `694-698`) → "$3 · 60 minutes, divisible, never expires".

- [ ] **Step 1:** Update the PAYG copy in all four surfaces (EN+RU), keeping `$3` and "never expires".
- [ ] **Step 2:** tsc + eslint + i18n keys present in both catalogs.
- [ ] **Step 3: Commit.** `git add apps/web/lib/plans.ts apps/web/components/app/UsageMeter.tsx apps/web/messages/en.json apps/web/messages/ru.json apps/web/lib/landingContent.ts` → `git commit -m "feat(payg-ui): present PAYG as a divisible 60-minute top-up (EN+RU)"`.

## Task 3b.6: Ship Phase 3b **[Opus orchestrator]**

- [ ] `just check` green. **Apply migration 0016 on Supabase FIRST, then immediately `modal deploy` the worker, then Vercel.** Verify a PAYG top-up on prod grants 60 divisible minutes and a short PAYG video deducts only its length. Update `docs/JOURNAL.md` + `docs/README.md` reality (PAYG = divisible minutes). Update the stale `migrations/0002_credits.sql:8` "$2/кредит" comment while here.

---

# Phase 4 — Studio $49 / 2520-min tier

**Decision:** Studio = **$49/mo**, `monthly_videos=42` → **2520 min** (marketed "2,520 min · ≈42 videos"; cleanly ≥ the studio's ">2000 min", per-min revenue $0.0194 = same as Pro, ~62% worst-case margin). Optional Agency $99/6000 is NOT in this plan (add later with the same steps).

**🔴 HARD ORDERING (live-safety):** the `profiles.plan` CHECK constraint is `('free','starter','pro')` (`0001:20`). A Studio `subscription.created` before the migration → `set_user_plan(user_id,"studio")` → PostgREST 400 → webhook 500 → Polar retry loop → paying customer never provisioned. **Run migration → deploy worker → create Polar product → deploy web, in that exact order.** SQLite dev has no CHECK, so local success does NOT prove prod safety.

## Task 4.1: Migration — widen `profiles.plan` CHECK **[Sonnet]**

**Files:** Create `services/worker/migrations/0017_studio_plan.sql`.

- [ ] **Step 1: Write the migration** (idempotent constraint swap):

```sql
-- 0017_studio_plan.sql — добавить план 'studio' в CHECK profiles.plan. Аддитивно и безопасно
-- для существующих free/starter/pro. ПРИМЕНИТЬ ДО деплоя воркера и создания Polar-продукта.
alter table public.profiles drop constraint if exists profiles_plan_check;
alter table public.profiles
  add constraint profiles_plan_check check (plan in ('free', 'starter', 'pro', 'studio'));
```

*(If the original constraint has a different name, `\d public.profiles` in SQL editor to confirm; the `drop constraint if exists` + re-add is safe either way.)*

- [ ] **Step 2: Commit.** `git add services/worker/migrations/0017_studio_plan.sql` → `git commit -m "feat(billing): migration 0017 — allow 'studio' plan"`.

## Task 4.2: Backend — Studio `PlanLimits` + Polar mapping (TDD) **[Opus]**

**Files:** Modify `services/worker/app/billing.py` (add `"studio"` to `PLANS`), `services/worker/app/polar.py` (`product_to_plan` + `parse_plan_change` + `_metadata_plan` accept studio), `services/worker/app/main.py` (webhook passes `product_studio`), `services/worker/app/config.py` (add `polar_product_studio`). Tests: `test_billing.py:230-235` (asserts the exact plan set — extend), `test_polar.py:100-101` + webhook test (signatures — extend).

**Interfaces:**
- `PLANS["studio"] = PlanLimits(id="studio", name="Studio", price_usd=49.0, monthly_videos=42, max_video_minutes=None, watermark=False, max_resolution=1080, video_crf=18, video_preset="medium", priority=True)`.
- `product_to_plan(product_id, *, starter, pro, studio) -> str | None` (add `studio` branch).
- `parse_plan_change(payload, *, product_starter, product_pro, product_studio)` (thread `studio` through).
- `_metadata_plan` set `("starter","pro")` → `("starter","pro","studio")`.

- [ ] **Step 1: Write failing tests** — `resolve_plan("studio").monthly_videos == 42` and `plan_monthly_minutes == 2520`; `product_to_plan("PID_STUDIO", starter="s", pro="p", studio="PID_STUDIO") == "studio"`; `parse_plan_change` on a studio `subscription.active` → `(uid, "studio")`; `_metadata_plan({"product":{"metadata":{"plan":"studio"}}}) == "studio"`.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the `PLANS` entry, the `product_to_plan`/`parse_plan_change`/`_metadata_plan` studio branches, the webhook call passing `product_studio=os.environ.get("POLAR_PRODUCT_STUDIO","")`, and `config.py` `polar_product_studio: str = ""`. Note `resolve_plan` already safely maps unknown→free, so a not-yet-deployed worker under-serves rather than crashing.
- [ ] **Step 4: Update the plan-set assertion tests** (`test_billing.py:230-235`) to include studio + keep the price-ordering invariant (`free < starter < pro < studio`).
- [ ] **Step 5: Run → pass;** `just check` green.
- [ ] **Step 6: Commit.** `git add services/worker/app/billing.py services/worker/app/polar.py services/worker/app/main.py services/worker/app/config.py services/worker/tests/unit/test_billing.py services/worker/tests/unit/test_polar.py` → `git commit -m "feat(billing): add Studio plan + Polar studio mapping"`.

## Task 4.3: Create the Polar product (orchestrator, via Polar MCP) **[Opus orchestrator]**

- [ ] **AFTER** migration 0017 is applied on Supabase AND the worker from Task 4.2 is deployed, create the Studio product in Polar (recurring, $49/mo, `metadata.plan=studio`) and capture its `product_id`. **Confirm with the founder before creating a live product.** Set the `POLAR_PRODUCT_STUDIO` Modal secret to that id and redeploy the worker so the webhook maps the product.
- [ ] Verify the webhook subscribes to `subscription.*` (it does — no change).

## Task 4.4: Frontend — Studio plan display + checkout **[Sonnet]**

**Files:** Modify `apps/web/lib/plans.ts` (`PlanId` union += `"studio"`; add the `PlanDisplay` entry, minutes-primary per Phase 2), `apps/web/lib/polar.ts` (`PRODUCT` record += `studio: process.env.NEXT_PUBLIC_POLAR_PRODUCT_STUDIO ?? "<studio product id>"` — **tsc forces this** once `PlanId` widens), `apps/web/components/marketing/PricingCards.tsx` (+ `landing/sections/Pricing.tsx`) layout → 4-up grid.

**Interfaces:** Studio `PlanDisplay` = `{ id:"studio", name:"Studio", price:49, tagline:"For studios & agencies.", limit:"2,520 minutes / month", limitNote:"≈42 hour-long videos. For teams doing daily long-form.", cta:"Choose Studio", features:[...] }`. Pro keeps `recommended:true` (or move to Studio — founder's call).

- [ ] **Step 1:** Widen `PlanId`; add the Studio `PlanDisplay` (EN; and Studio allowance in `landingContent.ts` EN+RU); add the `polar.ts` PRODUCT key with the id from Task 4.3.
- [ ] **Step 2:** Adjust `PricingCards.tsx` + landing `Pricing.tsx` grid from 3-up to 4-up (check responsive wrap).
- [ ] **Step 3:** Set Vercel env `NEXT_PUBLIC_POLAR_PRODUCT_STUDIO` (or rely on the hardcoded default).
- [ ] **Step 4:** tsc + eslint + anti-drift (`billing.py`⇄`plans.ts`) green.
- [ ] **Step 5: Commit.** `git add apps/web/lib/plans.ts apps/web/lib/polar.ts apps/web/components/marketing/PricingCards.tsx apps/web/components/landing/sections/Pricing.tsx apps/web/lib/landingContent.ts` → `git commit -m "feat(pricing): add Studio $49 tier to pricing + checkout"`.

## Task 4.5: Ship + verify Phase 4 **[Opus orchestrator]**

- [ ] Confirm ordering was honored: **0017 applied → worker deployed → Polar product live + `POLAR_PRODUCT_STUDIO` set → web deployed.**
- [ ] End-to-end: a test Studio checkout provisions `profiles.plan='studio'` (2520-min pool); dashboard shows the Studio allowance; a downgrade returns to the prior plan/free correctly.
- [ ] Update `docs/README.md` reality (plans: free/starter/pro/studio) + `docs/JOURNAL.md` + `billing.py` docstring economics line.

---

## Self-Review (run before execution)

**Spec coverage:** ✅ device→email (Phase 1), UI minutes-primary + stale copy (Phase 2), margin floor (3a), divisible PAYG (3b), Studio $49/2520 (Phase 4). Reset-anniversary alignment explicitly deferred.

**Placeholder scan:** billing-critical code (floor, PAYG split, Studio limits, migrations) is shown in full. Mechanical tasks give exact strings/values + the named function to mirror; the executing subagent reads the file. `<studio product id>` in Task 4.4 is resolved by Task 4.3 output (documented dependency, not a placeholder).

**Type consistency:** `list_jobs_for_user` (db+cloud), `listJobs()` (web), `payg_minutes` / `add_payg_minutes` / `deduct_payg_minutes` / `payg_minutes_for_split`, `product_studio` param name, `PlanId += "studio"` used consistently across tasks. `check_quota` signature unchanged (floor is internal). `_meter` untouched by 3a (reads the decision).

**Live-safety:** Phase 4 migration-first ordering called out twice; per-file `git add` mandated; PAYG transition window flagged with mitigation; unknown-plan→free fallback noted; RLS-bypass (service role) means the new endpoint filters by user_id itself.
