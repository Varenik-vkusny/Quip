# ⏭️ FOLLOW-UP: align quota reset to the billing anniversary (DEFERRED)

> **Status: NOT implemented — intentionally deferred (2026-07-01).** This is the ONE billing
> item left undone from the billing-clarity/studio branch. It is a money-path refactor with a
> **bounded** reward, so it was left out to avoid risk under a live paying customer. Read this
> before touching `billing.current_month` / `get_monthly_usage` / the Polar webhook.

## The leak (what's wrong today)

The monthly minute pool resets on the **calendar 1st** (`billing.current_month()` → `YYYY-MM`,
`services/worker/app/billing.py`), but Polar bills on the **subscription anniversary**, and the
renewal webhook only re-affirms `profiles.plan` — it never resets or tops up usage. So a user who
subscribes mid-cycle gets that calendar month's **full** pool immediately.

## How big is it (why deferred)

**Bounded, ~one-time.** A subscriber on day D gets 12 calendar pools for 12 payments/year —
balanced on average. The only real freebie is the **initial partial period** at signup (they get
the current calendar month's full pool but only paid from day D). Worst case ≈ **one plan pool
(~$4 COGS for Starter) once per mid-cycle signup** — NOT a persistent 2×. Low reward vs. the risk
of re-keying the billing accounting → deferred.

## It does NOT wipe anyone's minutes

The remaining balance is **computed, not stored**: `remaining = plan_pool − Σ(usage_events this
window)`. This change would only alter **when** the window rolls over, never delete usage. Any
rollout must be **gradual** (existing users keep calendar behavior until their next renewal sets
the anchor) so there is **no mass transition freebie**.

## Design (when someone does implement it)

1. **Migration** `0018_billing_cycle_anchor.sql`: `alter table public.profiles add column if not
   exists cycle_anchor smallint;` (nullable — the day-of-month 1–28 the subscription renews;
   `null` = free/no-sub → calendar fallback).
2. **Webhook** (`main.py` / `polar.py`): on active subscription events extract
   `current_period_start` (Polar payload), take `min(day, 28)`, store via a new
   `db.set_cycle_anchor(user_id, day)`. On cancel/revoke → set `null`.
3. **Pure fn** `billing.current_period_key(cycle_anchor: int | None, today: date) -> str`:
   - `None` → `f"{today.year}-{today.month:02d}"` (today's behavior, unchanged).
   - else → the most recent date `≤ today` whose day == `cycle_anchor`, as `"YYYY-MM-DD"` (if
     `today.day >= cycle_anchor` it's this month, else previous month). TDD this: calendar
     fallback, mid-month, month boundary, Jan→Dec wrap, the 28-clamp.
4. **`get_profile`** returns `cycle_anchor`; **callers** (`tasks._quota_gate` + `_meter`,
   `main._enforce_quota` + `get_usage`) replace `billing.current_month()` with
   `billing.current_period_key(profile["cycle_anchor"])`. **CRUCIAL:** the gate computes the key
   once and passes it to `_meter` via `holder[...]` so gate and meter use the SAME key (never
   let them diverge across a month boundary).
5. Keep `usage_events.month` as the key column (its VALUE just changes from `YYYY-MM` to a
   period-start date for subscribers). No historical rewrite.

## Alternative (simpler, if this proves fiddly)

Store `billing_anchor` = the literal `current_period_start` timestamp from each renewal webhook
and sum `usage_events` where `created_at >= billing_anchor`. Simpler window, but a missed renewal
webhook leaves a stale anchor (safe direction: under-serves, doesn't over-grant).
