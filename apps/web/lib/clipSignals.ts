import type { ClipOut } from "./types";

// Pure "quality signals" scoring for a clip card. Extracted from ClipCard.tsx so it can be
// unit-tested in the node vitest runner without pulling the whole "use client" component graph
// (next-intl / lucide / ExportMenu). ClipCard imports computeSignalBars + SignalKey from here.

export type SignalKey = "hook" | "standalone" | "energy" | "speaker";

/** Bars (1..4) for the ENERGY signal.
 *  Primary: the real per-clip audio loudness from the pipeline (`clip.energy`, 0..1) →
 *  `1 + round(energy·3)` → 1..4. Fallback (older clips with no energy on the contract): the legacy
 *  tone heuristic, which required an exact match against the UI's tone vocabulary. */
function energyBars(clip: ClipOut): number {
  if (clip.energy != null) return 1 + Math.round(clip.energy * 3); // 0..1 → 1..4
  // Back-compat: no measured energy → infer from tone (pre-energy clips).
  const tone = clip.tone ?? "";
  if (["shocking", "funny", "controversial"].includes(tone)) return 4;
  if (["inspiring", "relatable", "insightful"].includes(tone)) return 3;
  if (tone === "touching") return 2;
  return 1;
}

/** Returns translation KEYS (not labels) so the i18n catalog owns the copy; the rendering
 *  component resolves `signal.<key>` via next-intl. Pure scoring logic. */
export function computeSignalBars(clip: ClipOut): { key: SignalKey; bars: number }[] {
  const score = clip.score;
  const signals: { key: SignalKey; bars: number }[] = [
    {
      key: "hook",
      bars: score >= 0.85 ? 4 : score >= 0.7 ? 3 : score >= 0.55 ? 2 : 1,
    },
    {
      key: "standalone",
      bars: ["hook", "emotional_peak"].includes(clip.type)
        ? 4
        : clip.type === "complete_thought"
          ? 3
          : clip.type === "strong_quote"
            ? 3
            : 2,
    },
    {
      key: "energy",
      bars: energyBars(clip),
    },
    {
      key: "speaker",
      bars:
        (clip.hook?.length ?? 0) > 60
          ? 3 + (clip.why_works ? 1 : 0)
          : (clip.hook?.length ?? 0) > 30
            ? 2 + (clip.why_works ? 1 : 0)
            : 1 + (clip.why_works ? 1 : 0),
    },
  ];
  return signals.map((s) => ({ ...s, bars: Math.min(4, Math.max(1, s.bars)) }));
}
