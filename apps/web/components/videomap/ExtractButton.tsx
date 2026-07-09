"use client";

// The "extract a new clip from this moment/range" CTA. Milestone 1 only ships the `coming` state
// (disabled) — the real extraction endpoint + gate/paywall/expiry states land in M3/M4. The state
// machine is stubbed here so the visual language is ready and callers don't change later.
//
// Taste rule: only the LIVE (available) action earns the accent box. Every other state is a quiet
// text action, so the outline's right-hand column reads as one calm column ("Clip 3" / "Extract"),
// never a row of loud buttons.

export type ExtractState = "coming" | "available" | "gated" | "paywall" | "expired";

const LABEL: Record<ExtractState, string> = {
  coming: "Extract",
  available: "Extract",
  gated: "Extract",
  paywall: "Extract",
  expired: "Source expired",
};

const HINT: Record<ExtractState, string> = {
  coming: "Coming soon — turn this moment into a clip",
  available: "Turn this moment into a clip",
  gated: "Upgrade to Pro to extract clips",
  paywall: "You've used your free extractions for this video",
  expired: "The source is no longer available to re-cut",
};

// A small trailing tag for the upsell states only — normal case, faint, not an uppercase mono badge.
const TAG: Partial<Record<ExtractState, string>> = {
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
  const live = state === "available" && !isDisabled;

  if (live) {
    return (
      <button
        type="button"
        onClick={onExtract}
        title={HINT.available}
        aria-label={HINT.available}
        className="inline-flex items-center rounded-md border border-accent bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent transition-colors duration-150 hover:bg-accent/20"
      >
        Extract
      </button>
    );
  }

  const tag = TAG[state];
  return (
    <button
      type="button"
      disabled={isDisabled}
      onClick={isDisabled ? undefined : onExtract}
      title={HINT[state]}
      aria-label={HINT[state]}
      className={`inline-flex items-center gap-1 text-xs font-medium text-faint transition-colors duration-150 ${
        isDisabled ? "cursor-not-allowed" : "cursor-pointer hover:text-ink"
      }`}
    >
      {LABEL[state]}
      {tag && <span className="text-muted">{tag}</span>}
    </button>
  );
}
