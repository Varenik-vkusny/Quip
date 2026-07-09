import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { checkoutHref } from "@/lib/polar";
import { Container, Section } from "../components/primitives";
import { PrimaryCTA, GhostCTA } from "../components/CTA";
import { Reveal } from "../components/Reveal";
import { Check } from "@phosphor-icons/react/dist/ssr";

export async function Pricing() {
  const pricing = getLandingContent(resolveLocale(await getLocale())).pricing;
  const { heading, sub, recommended, plans, payg, footnote } = pricing;
  return (
    <Section id="pricing">
      <Container>
        <Reveal className="max-w-[44rem]">
          <h2 className="text-[clamp(30px,4vw,46px)] font-[650] leading-[1.06] tracking-[-0.024em] text-ink">
            {heading}
          </h2>
          <p className="mt-5 text-[1.0625rem] leading-relaxed text-muted">{sub}</p>
        </Reveal>

        <div className="mt-14 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {plans.map((plan, i) => {
            const pro = plan.recommended;
            const href = checkoutHref(plan.id);
            return (
              <Reveal key={plan.id} delay={i * 0.06}>
                <div
                  className={`relative flex h-full flex-col overflow-hidden rounded-card bg-surface p-7 sm:p-8 ${
                    pro
                      ? "shadow-[var(--t-shadow-card),inset_0_0_0_1.5px_var(--t-accent-line)]"
                      : "lift shadow-card"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <h3 className="text-[1.125rem] font-semibold tracking-[-0.01em] text-ink">{plan.name}</h3>
                    {pro && (
                      <span className="rounded-pill bg-accent px-2.5 py-1 text-[11px] font-semibold leading-none text-on-accent">
                        {recommended}
                      </span>
                    )}
                  </div>
                  <p className="mt-1.5 text-[13.5px] text-muted">{plan.blurb}</p>

                  <div className="mt-6 flex items-baseline gap-1.5">
                    <span className="num text-[42px] font-medium text-ink">{plan.price}</span>
                    <span className="font-mono text-[13px] text-muted">{plan.cadence}</span>
                  </div>
                  <p className="mt-2 text-[13px] text-faint">{plan.allowance}</p>

                  <ul className="mt-7 flex flex-1 flex-col gap-3">
                    {plan.features.map((feat) => (
                      <li key={feat} className="flex gap-2.5 text-[14px] leading-snug text-muted">
                        <Check
                          weight="bold"
                          className={`mt-0.5 size-3.5 shrink-0 ${pro ? "text-ok" : "text-faint"}`}
                        />
                        {feat}
                      </li>
                    ))}
                  </ul>

                  <div className="mt-8">
                    {pro ? (
                      <PrimaryCTA href={href} arrow={false} className="w-full">
                        {plan.cta}
                      </PrimaryCTA>
                    ) : (
                      <GhostCTA href={href} className="w-full">
                        {plan.cta}
                      </GhostCTA>
                    )}
                  </div>
                </div>
              </Reveal>
            );
          })}
        </div>

        {/* pay-as-you-go strip */}
        <Reveal delay={0.06} className="mt-4">
          <div className="lift flex flex-col items-start justify-between gap-4 rounded-[14px] bg-surface px-7 py-5 shadow-card sm:flex-row sm:items-center">
            <div className="flex items-baseline gap-3">
              <span className="text-[15px] font-semibold text-ink">{payg.name}</span>
              <span className="num text-[20px] font-medium text-ink">{payg.price}</span>
              <span className="font-mono text-[12px] text-muted">{payg.unit}</span>
            </div>
            <p className="text-[13.5px] text-muted">{payg.note}</p>
          </div>
        </Reveal>

        <p className="mt-6 text-center text-[13.5px] text-faint">{footnote}</p>
      </Container>
    </Section>
  );
}
