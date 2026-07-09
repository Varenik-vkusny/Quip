import { buildHomeJsonLd } from "@/lib/jsonld";
import { getOptionalUser } from "@/lib/supabase/server";
import { Audience } from "@/components/landing/sections/Audience";
import { Comparison } from "@/components/landing/sections/Comparison";
import { Craft } from "@/components/landing/sections/Craft";
import { Demo } from "@/components/landing/sections/Demo";
import { Faq } from "@/components/landing/sections/Faq";
import { FinalCta } from "@/components/landing/sections/FinalCta";
import { Footer } from "@/components/landing/sections/Footer";
import { Hero } from "@/components/landing/sections/Hero";
import { Manifesto } from "@/components/landing/sections/Manifesto";
import { MomentMap } from "@/components/landing/sections/MomentMap";
import { Nav } from "@/components/landing/sections/Nav";
import { Pricing } from "@/components/landing/sections/Pricing";
import { StudioWindow } from "@/components/landing/sections/StudioWindow";
import { ScrollFx } from "@/components/landing/fx/ScrollFx";

/*
  Home / marketing landing («Studio» redesign, 2026-07-09). Server Component;
  the single auth seam is read here: `authed` flows into Nav, Hero, and FinalCta
  only. Composition tells one story down the page:

    Hero (the product + its reasoning card + the waveform river)
    → Demo (a real run, end to end)
    → MomentMap (pinned film-strip: one video unfolded into moments)
    → Manifesto (the pivot line)
    → StudioWindow (the dark studio inside the light stage + 3 steps)
    → Craft (what we sweat) → Audience (who it's for) → Comparison (the wedge)
    → Pricing → FAQ → FinalCta.

  ScrollFx is the one client island wiring Lenis + GSAP choreography; every
  section stays server-rendered and readable without JS.
*/
export default async function Page() {
  const authed = Boolean(await getOptionalUser());

  return (
    <>
      <script
        type="application/ld+json"
        // Static, server-controlled data (site/plans/faq constants, no user input).
        // `<` is escaped so the JSON can never break out of the <script> context.
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(buildHomeJsonLd()).replace(/</g, "\\u003c"),
        }}
      />
      <ScrollFx />
      <Nav authed={authed} />
      <main className="relative">
        <Hero authed={authed} />
        <Demo />
        <MomentMap />
        <Manifesto />
        <StudioWindow />
        <Craft />
        <Audience />
        <Comparison />
        <Pricing />
        <Faq />
        <FinalCta authed={authed} />
      </main>
      <Footer />
    </>
  );
}
