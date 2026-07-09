import { Check, X } from "@phosphor-icons/react/dist/ssr";
import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, Section } from "../components/primitives";
import { ConfidenceGauge } from "../components/Confidence";
import { InlineClip } from "../components/InlineClip";
import { Reveal } from "../components/Reveal";

export async function Comparison() {
  const { comparison } = getLandingContent(resolveLocale(await getLocale()));
  const { heading, sub, rows, cols, kept, oneVideoNote, scoredTooLow, rejected } = comparison;
  return (
    <Section id="why">
      <Container>
        <Reveal className="max-w-[44rem]">
          <h2 className="text-[clamp(28px,3.6vw,44px)] font-[650] leading-[1.08] tracking-[-0.024em] text-ink">
            {heading}
          </h2>
          <p className="mt-4 text-[1.0625rem] text-muted">{sub}</p>
        </Reveal>

        {/* the cut: one kept clip vs the moments that scored too low to ship */}
        <Reveal delay={0.05} className="mt-12">
          <div className="grid items-center gap-10 rounded-[18px] bg-surface p-5 shadow-card sm:p-7 lg:grid-cols-[minmax(0,40fr)_minmax(0,60fr)] lg:gap-12">
            <div className="flex items-center gap-5">
              <div className="relative aspect-[9/16] w-[120px] shrink-0 overflow-hidden rounded-[10px] bg-bg shadow-pop">
                <InlineClip
                  src="/clips/clip-1.mp4"
                  poster="/clips/poster-1.jpg"
                  label="The clip that made the cut"
                  className="size-full object-cover"
                />
              </div>
              <div>
                <span className="inline-flex items-center gap-1.5 text-[13.5px] font-medium text-ok">
                  <Check weight="bold" className="size-3.5" />
                  {kept.label}
                </span>
                <ConfidenceGauge value={kept.score} variant="card" className="mt-3" />
                <p className="mt-3 max-w-[24ch] text-[13px] leading-snug text-muted">{oneVideoNote}</p>
              </div>
            </div>

            <div>
              <span className="text-[13.5px] text-faint">{scoredTooLow}</span>
              <ul className="mt-3 flex flex-col divide-y divide-line">
                {rejected.map((r) => (
                  <li key={r.label} className="flex items-baseline gap-4 py-2.5">
                    <span className="num w-8 shrink-0 text-right text-[15px] text-bad">{r.score}</span>
                    <span className="text-[14px] text-muted">{r.label}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </Reveal>

        {/* head to head: the Quip column reads as the winner (lifted surface), them dimmed */}
        <Reveal delay={0.05} className="mt-10">
          <div className="overflow-hidden rounded-[18px] border border-line">
            {/* header */}
            <div className="grid grid-cols-[1.1fr_1fr_1fr]">
              <div className="px-5 py-4 sm:px-6" />
              <div className="border-x border-line bg-surface-2 px-5 py-4 sm:px-6">
                <span className="flex items-center gap-2 text-[13px] font-medium text-ink">
                  <Check weight="bold" className="size-3.5 text-ok" />
                  {cols.quip}
                </span>
              </div>
              <div className="px-5 py-4 text-[13px] font-medium text-faint sm:px-6">{cols.them}</div>
            </div>
            {/* rows */}
            {rows.map((r) => (
              <div key={r.k} className="grid grid-cols-[1.1fr_1fr_1fr] border-t border-line text-[14px]">
                <div className="px-5 py-4 text-muted sm:px-6">{r.k}</div>
                <div className="flex items-start gap-2.5 border-x border-line bg-surface-2 px-5 py-4 font-medium text-ink sm:px-6">
                  {/* neutral ink check: the lifted surface-2 column already marks the winner;
                      per-row accent would over-spend the scarce gold (DESIGN.md). */}
                  <Check weight="bold" className="mt-0.5 size-3.5 shrink-0 text-ink" />
                  <span>{r.quip}</span>
                </div>
                <div className="flex items-start gap-2.5 px-5 py-4 text-faint sm:px-6">
                  <X weight="bold" className="mt-0.5 size-3.5 shrink-0 text-faint" />
                  <span>{r.them}</span>
                </div>
              </div>
            ))}
          </div>
        </Reveal>
      </Container>
    </Section>
  );
}
