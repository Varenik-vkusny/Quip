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
    "Simple, honest pricing for Quip. Your plan is a pool of minutes (1 video = 60 min), not a hard cap on uploads. Free, Starter ($15/mo), Pro ($35/mo), and Studio ($49/mo), plus pay-as-you-go at $3 a video. No surprise paywalls.",
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
          Your plan is a pool of minutes, not a cap on uploads — a short video uses only its length,
          so you can make far more than the headline number. No surprise paywalls. Start free.
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
