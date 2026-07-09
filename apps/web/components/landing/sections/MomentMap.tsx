import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, TypeBadge } from "../components/primitives";

/*
  MomentMap — the film-strip. On desktop ScrollFx pins the stage and converts
  vertical scroll into a horizontal pan across an hour of conversation: cards
  with timecodes, moment types, and confidence slide by like a timeline in an
  edit bay (this is the Video Map feature, drawn as theater). On mobile (and
  with reduced motion / no JS) the same strip is a native swipe gallery — no
  pinning tricks, no hidden content.
*/
export async function MomentMap() {
  const { momentMap, typeLabel } = getLandingContent(resolveLocale(await getLocale()));

  return (
    <section id="map" data-map-pin className="relative overflow-hidden">
      <div className="flex flex-col justify-center py-24 md:min-h-screen md:py-0">
        <Container>
          <div className="max-w-[46rem] pb-9 md:pb-10">
            <h2 className="text-display-lg text-ink [text-wrap:balance]">{momentMap.heading}</h2>
            <p className="mt-4 max-w-[56ch] text-[1.0625rem] leading-relaxed text-muted">
              {momentMap.sub}
            </p>
          </div>
        </Container>

        {/* the strip: pinned pan on md+, native swipe below */}
        <div className="overflow-x-auto no-scrollbar md:overflow-visible">
          <div
            data-map-track
            className="flex w-max snap-x snap-mandatory gap-5 px-6 md:snap-none md:gap-6 md:px-[max(24px,calc((100vw-1200px)/2+32px))]"
          >
            {momentMap.cards.map((card) => (
              <article
                key={card.tc}
                data-map-card
                className="w-[228px] shrink-0 snap-start overflow-hidden rounded-card bg-surface shadow-card will-change-transform md:w-[248px]"
              >
                <div className="relative aspect-[9/12] overflow-hidden bg-surface-3">
                  <img
                    src={card.poster}
                    alt=""
                    width={248}
                    height={330}
                    loading="lazy"
                    className="size-full object-cover object-[center_22%]"
                  />
                  <span className="absolute bottom-2.5 left-2.5 rounded-[6px] bg-[rgba(0,0,0,0.55)] px-1.5 py-0.5 font-mono text-[11px] text-[#fff]">
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

            {/* the tail: what's still unexplored on the map */}
            <div
              data-map-card
              className="grid w-[228px] shrink-0 snap-start place-items-center rounded-card shadow-[inset_0_0_0_1.5px_var(--t-line-strong)] will-change-transform md:w-[248px]"
            >
              <div className="px-6 text-center">
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
