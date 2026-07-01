/**
 * Marketing display data for pricing — MIRRORS services/worker/app/billing.py PLANS
 * (the enforcement source of truth). Keep numbers in sync when billing.py changes.
 *
 * Video-minutes model: 1 "video" = 60 min of source. A plan's monthly limit is a pool of
 * minutes, and the display now LEADS WITH MINUTES (the real quota) — the video count is a
 * friendly, derived ESTIMATE (minutes / 60), not a hard cap: a shorter video only uses its
 * own length, so the real number of videos you can make is often well above the nominal
 * count. No token casino, no surprise paywalls. A single video can be up to 3 hours; length
 * is limited only by your remaining minutes.
 */
export type PlanId = "free" | "starter" | "pro";

export interface PlanDisplay {
  id: PlanId;
  name: string;
  price: number; // USD / month
  tagline: string;
  /** Headline limit, shown large under the price (e.g. "600 minutes / month"). */
  limit: string;
  /** One line translating the minute pool into a friendly video estimate (not a hard cap). */
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
    limit: "120 minutes / month",
    limitNote: "That's about 2 videos — a short clip uses only its length, so you can make more.",
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
    limit: "600 minutes / month",
    limitNote: "About 10 hour-long videos — or 100 short ones. A 6-min clip uses just 6 minutes.",
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
    limit: "1,800 minutes / month",
    limitNote: "About 30 hour-long videos — or many more if they're shorter.",
    cta: "Choose Pro",
    recommended: true,
    features: [
      "Everything in Starter",
      "Priority processing in the queue",
      "Early access to new features",
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
