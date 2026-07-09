import { Check } from "lucide-react";
import { CheckoutCta } from "@/components/ui/CheckoutCta";
import { Reveal } from "@/components/ui/Reveal";
import { cn } from "@/lib/cn";
import { PAYG, PLANS } from "@/lib/plans";
import { checkoutHref, paygCheckoutHref } from "@/lib/polar";

// ────────────────────────────────────────────────────────────────────────────
// PricingCards (/pricing): plan panels plus a pay-as-you-go strip, in visual
// parity with the landing pricing section (components/landing/sections/Pricing.tsx):
// soft bg-surface cards with shadow-card + rounded-card, prices in mono numerals
// (.num), the accent spent only on the ONE recommended plan (inset ring + badge +
// gold CTA), everything else quiet. Non-recommended cards get the .lift hover.
// The --ease-out var is landing-scoped (.grain), so it is set locally here to keep
// .lift transitions eased outside the landing.
// ────────────────────────────────────────────────────────────────────────────

const EASE = "[--ease-out:cubic-bezier(0.23,1,0.32,1)]";

export function PricingCards() {
  return (
    <>
      <Reveal>
        <div
          className={cn(
            "mx-auto grid max-w-6xl items-stretch gap-4 sm:grid-cols-2 lg:grid-cols-4",
            EASE,
          )}
        >
          {PLANS.map((plan) => {
            const recommended = plan.recommended ?? false;
            return (
              <div
                key={plan.id}
                className={cn(
                  "relative flex h-full flex-col overflow-hidden rounded-card bg-surface p-7 sm:p-8",
                  recommended
                    ? "shadow-[var(--t-shadow-card),inset_0_0_0_1.5px_var(--t-accent-line)]"
                    : "lift shadow-card",
                )}
              >
                <div className="flex items-center justify-between">
                  <h2 className="text-[1.125rem] font-semibold tracking-[-0.01em] text-ink">
                    {plan.name}
                  </h2>
                  {recommended && (
                    <span className="rounded-pill bg-accent px-2.5 py-1 text-[11px] font-semibold leading-none text-on-accent">
                      Recommended
                    </span>
                  )}
                </div>
                <p className="mt-1.5 text-[13.5px] text-muted">{plan.tagline}</p>

                <div className="mt-6 flex items-baseline gap-1.5">
                  <span className="num text-[42px] font-medium text-ink">${plan.price}</span>
                  <span className="font-mono text-[13px] text-muted">/ month</span>
                </div>
                <p className="mt-2 text-[13px] text-faint">{plan.limit}</p>
                <p className="mt-1 text-xs leading-snug text-faint">{plan.limitNote}</p>

                <ul className="mt-7 flex flex-1 flex-col gap-3">
                  {plan.features.map((f) => (
                    <li key={f} className="flex gap-2.5 text-[14px] leading-snug text-muted">
                      <Check
                        className={cn(
                          "mt-0.5 size-3.5 shrink-0",
                          recommended ? "text-ok" : "text-faint",
                        )}
                        aria-hidden
                      />
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>

                {/* Recommended CTA is the one gold action (bg-accent text-on-accent via
                    the accent button variant); everything else stays quiet. */}
                <CheckoutCta
                  href={checkoutHref(plan.id)}
                  variant={recommended ? "accent" : "secondary"}
                  size="md"
                  className="mt-8 w-full"
                >
                  {plan.cta}
                </CheckoutCta>
              </div>
            );
          })}
        </div>
      </Reveal>

      {/* Pay-as-you-go strip: no subscription, credits never expire. */}
      <Reveal>
        <div
          className={cn(
            "lift mx-auto mt-4 flex max-w-6xl flex-col items-start justify-between gap-4 rounded-[14px] bg-surface px-7 py-5 shadow-card sm:flex-row sm:items-center",
            EASE,
          )}
        >
          <div>
            <p className="text-[15px] font-semibold text-ink">{PAYG.title}</p>
            <p className="mt-1 max-w-md text-[13.5px] leading-relaxed text-muted">{PAYG.body}</p>
          </div>
          <div className="flex shrink-0 items-center gap-5">
            <div className="flex items-baseline gap-1.5">
              <span className="num text-[20px] font-medium text-ink">${PAYG.pricePerVideo}</span>
              <span className="font-mono text-[12px] text-muted">/ video</span>
            </div>
            <CheckoutCta href={paygCheckoutHref()} variant="secondary" size="md">
              {PAYG.cta}
            </CheckoutCta>
          </div>
        </div>
      </Reveal>

      <p className="mt-6 text-center text-[13.5px] text-faint">
        Prices in USD · cancel anytime · monthly credits reset · pay-as-you-go never expires
      </p>
    </>
  );
}
