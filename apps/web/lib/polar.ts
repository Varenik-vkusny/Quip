/**
 * Polar.sh checkout wiring (frontend). Dynamic checkout via the `/checkout` route handler
 * (@polar-sh/nextjs) — NOT static links. Plan/PAYG CTAs link to `/checkout?products=<id>`;
 * the route injects customerExternalId = Supabase user.id and redirects to Polar.
 *
 * Dual-mode: until POLAR_ACCESS_TOKEN is set (founder), the route funnels to /signup
 * (no dead buttons). Product IDs are public (not secrets); env can override.
 */
import type { PlanId } from "@/lib/plans";

/** Polar product IDs (founder's Quip products). Public; override via NEXT_PUBLIC_* if needed. */
const PRODUCT: Record<Exclude<PlanId, "free"> | "payg" | "extraClip", string> = {
  starter:
    process.env.NEXT_PUBLIC_POLAR_PRODUCT_STARTER ?? "64607ae8-4d14-4eb2-a873-4c07a88cc83e",
  pro: process.env.NEXT_PUBLIC_POLAR_PRODUCT_PRO ?? "3d989817-94b9-41ee-958b-d1b56013c980",
  studio:
    process.env.NEXT_PUBLIC_POLAR_PRODUCT_STUDIO ?? "708186c8-52eb-4d69-a2f7-dbcaa818e1e2",
  payg: process.env.NEXT_PUBLIC_POLAR_PRODUCT_PAYG ?? "e7966387-a84b-4e2e-847d-df99862cf2b6",
  // $1 one-time "Extra clip" — grants one clip_credit, consumed when extracting a clip past the
  // free cap on the Video map. Separate balance from PAYG video credits (worker: parse_extra_clip_order).
  extraClip:
    process.env.NEXT_PUBLIC_POLAR_PRODUCT_EXTRA_CLIP ?? "4ccc3b3b-8f6d-422d-b529-79a5d4bf5738",
};

/** Where a plan's CTA goes. Free → signup; paid → dynamic /checkout (which dual-modes to
 *  /signup until POLAR_ACCESS_TOKEN is wired). */
export function checkoutHref(planId: PlanId): string {
  if (planId === "free") return "/signup";
  return `/checkout?products=${PRODUCT[planId]}`;
}

/** Pay-as-you-go CTA → dynamic /checkout for the one-off credit product. */
export function paygCheckoutHref(): string {
  return `/checkout?products=${PRODUCT.payg}`;
}

/** "Pay $1 for one extra clip" CTA (Video map paywall, past the free cap) → dynamic /checkout for
 *  the one-off Extra-clip product. The webhook grants one clip_credit; the next extract consumes it. */
export function extraClipCheckoutHref(): string {
  return `/checkout?products=${PRODUCT.extraClip}`;
}
