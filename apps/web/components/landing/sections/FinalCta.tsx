import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent, ROUTES } from "@/lib/landingContent";
import { Container } from "../components/primitives";
import { PrimaryCTA, GhostCTA } from "../components/CTA";
import { Reveal } from "../components/Reveal";

export async function FinalCta({ authed = false }: { authed?: boolean }) {
  const { finalCta, openApp } = getLandingContent(resolveLocale(await getLocale()));
  return (
    <section className="py-28 md:py-36">
      <Container>
        <Reveal className="mx-auto max-w-[40rem] text-center">
          <h2 className="text-[clamp(34px,5vw,60px)] font-[650] leading-[1.04] tracking-[-0.026em] text-ink">
            {finalCta.heading}
          </h2>
          <p className="mx-auto mt-6 max-w-[48ch] text-[1.0625rem] leading-relaxed text-muted">{finalCta.sub}</p>

          <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
            <PrimaryCTA href={authed ? ROUTES.app : ROUTES.signup} size="lg">
              {authed ? openApp : finalCta.primary}
            </PrimaryCTA>
            <GhostCTA href="#pricing" size="lg">
              {finalCta.secondary}
            </GhostCTA>
          </div>

          <p className="mt-7 text-[13.5px] text-faint">{finalCta.trust}</p>
        </Reveal>
      </Container>
    </section>
  );
}
