import { Nav } from "./sections/Nav";
import { Hero } from "./sections/Hero";
import { Demo } from "./sections/Demo";
import { CostAnchor } from "./sections/CostAnchor";
import { HowItWorks } from "./sections/HowItWorks";
import { WhyQuip } from "./sections/WhyQuip";
import { Craft } from "./sections/Craft";
import { Comparison } from "./sections/Comparison";
import { Pricing } from "./sections/Pricing";
import { Faq } from "./sections/Faq";
import { FinalCta } from "./sections/FinalCta";
import { Footer } from "./sections/Footer";

// Auth-aware slot. A real build reads session here; logged-out is the safe default.
const AUTHED = false;

export default function Page() {
  return (
    <>
      <Nav authed={AUTHED} />
      <main className="relative">
        <Hero authed={AUTHED} />
        <Demo />
        <CostAnchor />
        <HowItWorks />
        <WhyQuip />
        <Craft />
        <Comparison />
        <Pricing />
        <Faq />
        <FinalCta authed={AUTHED} />
      </main>
      <Footer />
    </>
  );
}
