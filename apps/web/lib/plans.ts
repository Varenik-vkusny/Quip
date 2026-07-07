/**
 * Marketing display data for pricing — MIRRORS services/worker/app/billing.py PLANS
 * (the enforcement source of truth). Keep numbers in sync when billing.py changes.
 *
 * Video-minutes model: 1 "video" = 60 min of source. A plan's monthly limit is a pool of
 * minutes (videos × 60); a longer video just uses minutes proportionally (90 min = 1.5
 * videos), so the balance is shown as both videos and minutes. No token casino, no surprise
 * paywalls. A single video can be up to 3 hours; length is limited only by your remaining minutes.
 */
export type PlanId = "free" | "starter" | "pro" | "studio";

export interface PlanDisplay {
  id: PlanId;
  name: string;
  price: number; // USD / month
  tagline: string;
  /** Headline limit, shown large under the price (e.g. "10 videos / month"). */
  limit: string;
  /** One line clarifying what a "video" means on this plan. */
  limitNote: string;
  cta: string;
  /** The recommended plan gets the accent treatment (badge + ring + coral CTA). */
  recommended?: boolean;
  features: string[];
}

export const PLANS: PlanDisplay[] = [
  {
    id: "free",
    name: "Free",
    price: 0,
    tagline: "See it work, no card required.",
    limit: "2 videos / month",
    limitNote: "120 minutes of source to use however you like.",
    cta: "Start free",
    features: [
      "Explainable clips with hook, score and reason",
      "Smooth 9:16 reframe that tracks the speaker",
      "Full editor and every caption style",
      "1080p export with a Quip watermark",
    ],
  },
  {
    id: "starter",
    name: "Starter",
    price: 15,
    tagline: "For creators shipping weekly.",
    limit: "10 videos / month",
    limitNote: "600 min total. Longer videos use minutes proportionally.",
    cta: "Choose Starter",
    features: [
      "Everything in Free",
      "No watermark, 1080p export",
      "All aspect ratios: 9:16, 1:1, 4:5, 16:9",
      "Top up anytime with pay-as-you-go videos",
    ],
  },
  {
    id: "pro",
    name: "Pro",
    price: 35,
    tagline: "For heavy repurposing.",
    limit: "30 videos / month",
    limitNote: "1800 min total. Longer videos use minutes proportionally.",
    cta: "Choose Pro",
    recommended: true,
    features: [
      "Everything in Starter",
      "Priority processing in the queue",
      "Early access to new features",
      "Pay-as-you-go videos when you need more",
    ],
  },
  {
    id: "studio",
    name: "Studio",
    price: 49,
    tagline: "For studios & agencies shipping daily.",
    limit: "42 videos / month",
    limitNote: "2520 min total. Longer videos use minutes proportionally.",
    cta: "Choose Studio",
    features: [
      "Everything in Pro",
      "42 videos every month for high-volume teams",
      "Priority processing in the queue",
      "Pay-as-you-go videos when you need more",
    ],
  },
];

/** Pay-as-you-go: one-off videos, no subscription. Mirrors billing.PAYG_PRICE_USD. */
export const PAYG = {
  pricePerVideo: 3, // USD per video (source up to 60 min)
  title: "No subscription? Pay as you go.",
  body: "Buy videos one at a time. They never expire — one video covers up to 60 minutes of source.",
  cta: "Buy a video",
} as const;

/**
 * On-demand clip extraction gate + economics. MIRRORS services/worker/app/billing.py
 * (`can_extract_clip` / `extract_requires_payment`) and app/config.py (`extract_free_cap` /
 * `extract_clip_price_usd`). The worker enforces these server-side; this mirror only drives which
 * Extract-button state the UI shows (available / upgrade / paywall). Money path — keep in sync
 * with billing.py (CLAUDE.md).
 */
export const EXTRACT = {
  /** Plans allowed to extract new clips from un-clipped moments (Free/Starter see the map only). */
  plans: ["pro", "studio"] as PlanId[],
  /** Free while total clips (auto + extracted) stay under this; at/above it → per-clip paywall. */
  freeCap: 30,
  /** Price of one extra clip beyond the free cap, USD. */
  clipPriceUsd: 1,
} as const;

/** True if the plan may extract clips. */
export function canExtractClip(plan: string | null | undefined): boolean {
  return !!plan && (EXTRACT.plans as readonly string[]).includes(plan);
}
