import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, Section } from "../components/primitives";
import { Reveal } from "../components/Reveal";

/*
  "Who it's for" - the ICP section. Deliberately NOT a 3-column icon-in-circle grid
  (DESIGN.md anti-slop). It reads as an instrument ledger instead: one card panel,
  one row per audience, each row a [who + the win] rail against a [grind today | with Quip]
  split. Structure encodes the real argument (the drudgery Quip removes), not decoration.
  No accent is spent in this section; the headline opens it on its own. The privacy
  strip is the one trust block the funnel lacked.
*/
export async function Audience() {
  const { audience } = getLandingContent(resolveLocale(await getLocale()));
  return (
    <Section id="who">
      <Container>
        <Reveal className="max-w-[44rem]">
          <h2 className="text-[clamp(28px,3.6vw,44px)] font-[650] leading-[1.08] tracking-[-0.024em] text-ink">
            {audience.heading}
          </h2>
          <p className="mt-5 text-[1.0625rem] leading-relaxed text-muted">{audience.sub}</p>
        </Reveal>

        <Reveal delay={0.05} className="mt-12">
          <div className="overflow-hidden rounded-[18px] bg-surface shadow-card">
            {audience.roles.map((r, i) => (
              <div
                key={r.id}
                className={`grid gap-x-10 gap-y-5 p-7 sm:p-8 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)] ${
                  i > 0 ? "border-t border-line" : ""
                }`}
              >
                {/* left rail: who they are + the win they get */}
                <div className="flex flex-col">
                  <h3 className="text-[1.3rem] font-semibold tracking-[-0.015em] text-ink">{r.who}</h3>
                  <span className="mt-1.5 text-[13.5px] text-muted">{r.role}</span>
                </div>

                {/* right: the grind today vs what Quip hands back */}
                <div className="grid gap-5 md:grid-cols-2">
                  <div>
                    <span className="text-[12.5px] font-medium text-faint">{audience.grindLabel}</span>
                    <p className="mt-2 text-[14.5px] leading-relaxed text-muted">{r.grind}</p>
                  </div>
                  <div className="md:border-l md:border-line md:pl-5">
                    <span className="text-[12.5px] font-medium text-muted">{audience.gainLabel}</span>
                    <p className="mt-2 text-[14.5px] leading-relaxed text-ink">{r.gain}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* privacy: the one trust block the funnel was missing */}
          <div className="mt-4 rounded-[12px] border border-line px-5 py-4 sm:px-6 sm:py-5">
            <span className="text-[12.5px] font-medium text-faint">{audience.privacyLabel}</span>
            <p className="mt-1.5 max-w-[62ch] text-[13.5px] leading-relaxed text-muted">{audience.trust}</p>
          </div>
        </Reveal>
      </Container>
    </Section>
  );
}
