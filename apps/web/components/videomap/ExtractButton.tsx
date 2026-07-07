"use client";

// The "extract a new clip from this moment/range" CTA. Milestone 1 only ships the `coming` state
// (disabled, "soon") — the real extraction endpoint + gate/paywall/expiry states land in M3/M4. The
// state machine is stubbed here now so the visual language is ready and callers don't change later.

export type ExtractState = "coming" | "available" | "gated" | "paywall" | "expired";

const LABEL: Record<ExtractState, string> = {
  coming: "Extract clip",
  available: "Extract clip",
  gated: "Extract clip",
  paywall: "Extract clip",
  expired: "Source expired",
};

const HINT: Record<ExtractState, string> = {
  coming: "Coming soon — turn this moment into a clip",
  available: "Turn this moment into a clip",
  gated: "Upgrade to Pro to extract clips",
  paywall: "You've used your free extractions this video",
  expired: "The source is no longer available to re-cut",
};

// A trailing status word so the button reads as intentional, not broken (M1 = "soon").
const BADGE: Partial<Record<ExtractState, string>> = {
  coming: "soon",
  gated: "Pro",
  paywall: "$",
};

export function ExtractButton({
  disabled,
  onExtract,
  state = "coming",
}: {
  disabled?: boolean;
  onExtract?: () => void;
  state?: ExtractState;
}) {
  const isDisabled = disabled ?? (state === "coming" || state === "expired");
  const badge = BADGE[state];

  // Live (M3+) affordance vs. inert placeholder. Coral is reserved for the available CTA; every
  // other state stays quiet on the warm ladder so it never competes with the "Open clip" links.
  const live = state === "available" && !isDisabled;
  const tone = live
    ? "border-accent bg-accent/10 text-accent hover:bg-accent/20"
    : "border-line bg-surface-2 text-muted";

  return (
    <button
      type="button"
      disabled={isDisabled}
      onClick={isDisabled ? undefined : onExtract}
      title={HINT[state]}
      aria-label={HINT[state]}
      className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-semibold transition-colors duration-150 ${tone} ${
        isDisabled ? "cursor-not-allowed opacity-70" : "cursor-pointer"
      }`}
    >
      {LABEL[state]}
      {badge && (
        <span className="font-mono text-eyebrow uppercase tabular-nums text-faint">{badge}</span>
      )}
    </button>
  );
}
