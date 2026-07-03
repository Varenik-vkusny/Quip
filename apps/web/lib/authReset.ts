/**
 * Pure reconciliation for the cross-account client-cache purge (defense-in-depth for the
 * IDOR / cross-account-leak fix). The worker's ownership gate is the real security boundary;
 * this layer only prevents account B, on a shared device, from seeing account A's *client*
 * leftovers (the old global recents blob, editor inspector width, another user's reviewed set).
 *
 * Kept pure (no DOM) so it's unit-tested in isolation; the actual localStorage sweep lives in
 * the client components (SignOutButton, AuthStateSync) and calls into `keysToPurge`.
 */

/** Legacy GLOBAL recents blob (never per-user, never cleared) — the original cross-account leak. */
export const LEGACY_RECENT_KEY = "quip:recent-projects";
/** Editor inspector column width — global, harmless but user-specific; wiped on account change. */
export const INSPECTOR_WIDTH_KEY = "quip:inspectorW";
/** Marker of the last user id seen on this device (drives the account-switch detection). */
export const LAST_USER_ID_KEY = "quip:lastUserId";
/** Prefix of the PER-USER reviewed-set keys (`quip:reviewed:<userId>`). Booleans only, not a leak. */
export const REVIEWED_KEY_PREFIX = "quip:reviewed:";

/**
 * Should cross-account client caches be purged given the previously-seen and current user ids?
 *
 * Purge when the identities differ or the current identity is absent (sign-out / no session):
 *   - same non-null user (e.g. a token refresh emits a new session for the SAME id) → keep
 *   - different id, first login on this device (null → id), or sign-out (id → null) → purge
 */
export function shouldPurge(lastUserId: string | null, currentUserId: string | null): boolean {
  if (currentUserId === null) return true; // signed out / no session → purge
  return lastUserId !== currentUserId; // different (or first-seen) user → purge
}

/**
 * Given ALL localStorage keys present, return the subset to remove on a sign-out / account switch:
 * the legacy global recents blob, the editor inspector width, and EVERY user's reviewed set
 * (cheap booleans — we purge all of them defensively since we don't always know the prior id).
 * The `LAST_USER_ID_KEY` marker is intentionally preserved here — the caller rewrites it. PURE.
 */
export function keysToPurge(allKeys: string[]): string[] {
  return allKeys.filter(
    (k) =>
      k === LEGACY_RECENT_KEY ||
      k === INSPECTOR_WIDTH_KEY ||
      k.startsWith(REVIEWED_KEY_PREFIX),
  );
}
