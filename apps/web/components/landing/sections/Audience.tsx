import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, Eyebrow, Section } from "../components/primitives";
import { Reveal } from "../components/Reveal";

/*
  "Who it's for" — the ICP section. Deliberately NOT a 3-column icon-in-circle grid
  (DESIGN.md anti-slop). It reads as an instrument ledger instead: one hairline panel,
  one row per audience, each row a [who + the win] rail against a [grind today | with Quip]
  split. Structure encodes the real argument (the drudgery Quip removes), not decoration.
  Coral is spent once, on the Eyebrow tick — role tags stay ink/muted to keep the accent
  scarce (DESIGN.md coral rule). The privacy strip is the one trust block the funnel lacked.
*/
export async function Audience() {
  const { audience } = getLandingContent(resolveLocale(await getLocale()));
  return (
    <Section id="who">
      <Container>
        <Reveal className="max-w-[44rem]">
          <Eyebrow>{audience.eyebrow}</Eyebrow>
          <h2 className="mt-5 text-[clamp(28px,3.6vw,44px)] font-bold leading-[1.08] tracking-[-0.025em] text-ink">
            {audience.heading}
          </h2>
          <p className="mt-5 text-[1.0625rem] leading-relaxed text-muted">{audience.sub}</p>
        </Reveal>

        <Reveal delay={0.05} className="mt-12">
          <div className="overflow-hidden rounded-[18px] border border-line-strong bg-surface">
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
                  <span className="mt-2 font-mono text-[11px] uppercase tracking-[0.14em] text-muted">
                    {r.role}
                  </span>
                </div>

                {/* right: the grind today → what Quip hands back */}
                <div className="grid gap-5 md:grid-cols-2">
                  <div>
                    <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-faint">
                      {audience.grindLabel}
                    </span>
                    <p className="mt-2 text-[14.5px] leading-relaxed text-muted">{r.grind}</p>
                  </div>
                  <div className="md:border-l md:border-line md:pl-5">
                    <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-ink/70">
                      {audience.gainLabel}
                    </span>
                    <p className="mt-2 text-[14.5px] leading-relaxed text-ink">{r.gain}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* privacy: the one trust block the funnel was missing */}
          <div className="mt-4 rounded-[12px] border border-line bg-surface/50 px-5 py-4 sm:px-6 sm:py-5">
            <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-faint">
              {audience.privacyLabel}
            </span>
            <p className="mt-1.5 max-w-[62ch] text-[13.5px] leading-relaxed text-muted">{audience.trust}</p>
          </div>
        </Reveal>
      </Container>
    </Section>
  );
}
