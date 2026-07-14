import { getLocale, getTranslations } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, TypeBadge } from "../components/primitives";

/*
  MomentMap — "the Reel". The hour of conversation, unfolded into a 3D corridor
  of clips receding into space; ScrollFx pins the section and flies the camera
  along it, igniting each moment (its timecode chip flips to gold) as it passes
  the focal plane. Pure CSS-3D transforms, no WebGL.

  Progressive enhancement: this markup is, by default, a readable horizontal
  swipe strip (the same cards). On md+ with motion allowed, ScrollFx adds
  `.reel-on` and turns it into the flythrough. With reduced motion / no JS /
  crawlers / mobile, it stays the swipe strip — no pinning tricks, no hidden
  content.
*/
export async function MomentMap() {
  const { momentMap, typeLabel } = getLandingContent(resolveLocale(await getLocale()));
  const t = await getTranslations("landingDemo");

  return (
    <section id="map" data-reel-pin className="reel relative overflow-hidden">
      <div className="reel-vignette" aria-hidden />

      <div className="flex flex-col md:h-screen">
        <Container className="relative z-[4] pt-24 md:pt-20">
          <div className="max-w-[46rem]">
            <h2 className="text-display-lg text-ink [text-wrap:balance]">{momentMap.heading}</h2>
            <p className="mt-4 max-w-[56ch] text-[1.0625rem] leading-relaxed text-muted">
              {momentMap.sub}
            </p>
          </div>
        </Container>

        <div className="reel-stage mt-10 flex-1 pb-24 md:mt-0 md:pb-0">
          <div data-reel-scene className="reel-scene">
            {momentMap.cards.map((card) => (
              <article
                key={card.tc}
                data-reel-card
                className="reel-card group overflow-hidden rounded-card bg-surface shadow-card"
              >
                <div className="relative aspect-[9/12] overflow-hidden bg-surface-3">
                  {/* NOT decorative: these are stills of real clips and they carry the product's
                      whole claim. An empty alt hid them from search and from screen readers alike.
                      card.title is already localized (landingContent), so the alt is too. */}
                  <img
                    src={card.poster}
                    alt={t("clipAlt", { caption: card.title })}
                    width={224}
                    height={298}
                    loading="lazy"
                    className="size-full object-cover object-[center_22%]"
                  />
                  <span className="reel-tc absolute bottom-2.5 left-2.5 rounded-[6px] bg-[rgba(0,0,0,0.6)] px-1.5 py-0.5 font-mono text-[11px] text-[#fff]">
                    {card.tc}
                  </span>
                </div>
                <div className="p-3.5">
                  <div className="flex items-center justify-between gap-2">
                    <TypeBadge type={card.type} label={typeLabel[card.type]} />
                    <span className="font-mono text-[12px] font-semibold text-ok">
                      {card.confidence}
                    </span>
                  </div>
                  <h3 className="mt-2.5 text-[14px] font-semibold leading-[1.35] tracking-[-0.01em] text-ink">
                    {card.title}
                  </h3>
                </div>
              </article>
            ))}

            {/* the tail: the corridor runs on past what's shown */}
            <div
              data-reel-card
              className="reel-card grid place-items-center rounded-card shadow-[inset_0_0_0_1.5px_var(--t-line-strong)]"
            >
              <div className="px-6 py-16 text-center">
                <div className="font-mono text-[30px] font-semibold text-faint">
                  {momentMap.more.count}
                </div>
                <p className="mt-2 text-[13px] leading-relaxed text-faint">{momentMap.more.note}</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
