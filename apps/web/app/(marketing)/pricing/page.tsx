import type { Metadata } from "next";
import { Suspense } from "react";
import { CheckoutNotice } from "@/components/marketing/CheckoutNotice";
import { Comparison } from "@/components/marketing/Comparison";
import { Faq } from "@/components/marketing/Faq";
import { PricingCards } from "@/components/marketing/PricingCards";
import { Container } from "@/components/ui/Container";
import { Eyebrow } from "@/components/ui/Eyebrow";

export const metadata: Metadata = {
  title: "Pricing",
  description:
    "Simple, honest pricing for Quip. One credit is one video (up to 60 min). Free, Starter ($15/mo), Pro ($35/mo), and Studio ($49/mo for studios and agencies), plus pay-as-you-go at $3 a video. No credit casino, no surprise paywalls.",
  alternates: { canonical: "/pricing" },
};

export default function PricingPage() {
  return (
    <>
      <Suspense fallback={null}>
        <CheckoutNotice />
      </Suspense>
      <Container className="pt-16 text-center sm:pt-24">
        <Eyebrow tone="faint" className="inline-flex items-center gap-2">
          <span className="h-px w-6 bg-line-strong" aria-hidden />
          Pricing
          <span className="h-px w-6 bg-line-strong" aria-hidden />
        </Eyebrow>
        <h1 className="mx-auto mt-4 max-w-2xl font-display text-display-lg text-ink sm:text-display-xl">
          Pricing that respects your wallet.
        </h1>
        <p className="mx-auto mt-4 max-w-lg text-lead text-muted">
          One credit is one video, up to 60 minutes. No tokens to ration, no surprise paywalls, no
          dark patterns. Start free.
        </p>
        <p className="mx-auto mt-3 max-w-md text-sm text-faint">
          For studios and agencies, Studio works out to just over a dollar a video, less than a single
          freelance clip.
        </p>
      </Container>
      <Container className="pb-8 pt-14">
        <PricingCards />
      </Container>
      <Comparison />
      <Faq />
    </>
  );
}
