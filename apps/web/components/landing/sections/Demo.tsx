import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, Section } from "../components/primitives";
import { InlineClip } from "../components/InlineClip";
import { Reveal } from "../components/Reveal";

export async function Demo() {
  const demo = getLandingContent(resolveLocale(await getLocale())).demo;
  return (
    <Section id="demo">
      <Container>
        <Reveal className="max-w-[44rem]">
          <h2 className="text-[clamp(30px,4vw,46px)] font-[650] leading-[1.06] tracking-[-0.024em] text-ink">
            {demo.heading}
          </h2>
          <p className="mt-5 max-w-[58ch] text-[1.0625rem] leading-relaxed text-muted">{demo.sub}</p>
        </Reveal>

        {/* the real run: one framed surface, one depth recipe */}
        <Reveal delay={0.08} className="mt-12">
          <div className="overflow-hidden rounded-[18px] bg-surface p-2 shadow-pop sm:p-3">
            <div className="relative aspect-[1600/822] w-full overflow-hidden rounded-[12px] bg-bg">
              <InlineClip
                src="/media/demo.mp4"
                poster="/media/demo-poster-1400.jpg"
                label="A real Quip run: upload, find the moments, cut vertical clips"
                className="size-full object-cover"
              />
            </div>
          </div>
        </Reveal>
      </Container>
    </Section>
  );
}
