import { getLocale, getTranslations } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent, ROUTES } from "@/lib/landingContent";
import { Container, TypeBadge } from "../components/primitives";
import { PrimaryCTA, GhostCTA } from "../components/CTA";
import { InlineClip } from "../components/InlineClip";
import { WaveformRiver, type RiverMoment } from "../fx/WaveformRiver";

/*
  Hero — the light «stage». Left: the value prop, the biggest, highest-contrast
  type on the whole page (it must out-scale every H2 below it). Right: a SOLID,
  always-visible product cluster — the playing vertical clip, a second clip
  peeking for depth, and the reasoning card floating over it (the un-copyable
  artifact: hook + moment type + confidence). Below, the signature WaveformRiver.

  Robustness rule (learned the hard way): hero content is VISIBLE BY DEFAULT.
  Entrance motion is a load-triggered `.hero-rise` CSS animation, NOT a whileInView
  reveal — an IntersectionObserver reveal stalls in background/bfcache/headless
  tabs and ships the hero blank. ScrollFx still layers parallax ([data-para]) and
  the confidence count-up ([data-count-to]) on top; both degrade to the static
  rendered values if the JS never runs.
*/
export async function Hero({ authed = false }: { authed?: boolean }) {
  const content = getLandingContent(resolveLocale(await getLocale()));
  const { hero, heroClip, typeLabel, openApp } = content;
  const t = await getTranslations("heroReadout");

  // The river's moments: fixed positions across the "hour", labeled with the
  // localized moment types. Decorative (aria-hidden), so no content coupling.
  const riverMoments: RiverMoment[] = [
    { at: 0.16, type: "hook", label: typeLabel.hook },
    { at: 0.38, type: "quote", label: typeLabel.quote },
    { at: 0.61, type: "peak", label: typeLabel.peak },
    { at: 0.84, type: "thought", label: typeLabel.thought },
  ];

  return (
    <section id="top" data-hero className="relative overflow-hidden pt-28 md:pt-36">
      {/* one atmospheric wash: gold breath top-left, faint peak-violet far right */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(920px 520px at 10% -12%, var(--t-accent-tint), transparent 60%)," +
            "radial-gradient(720px 440px at 92% 6%, var(--t-peak-tint), transparent 58%)",
          opacity: 0.9,
        }}
      />

      <Container className="relative">
        <div className="grid items-center gap-y-16 lg:grid-cols-[minmax(0,53fr)_minmax(0,47fr)] lg:gap-x-14">
          {/* LEFT — value prop. The type here is the loudest on the page. */}
          <div className="max-w-[38rem]">
            <h1
              className="hero-rise text-[clamp(2.5rem,5.2vw,4.5rem)] font-[650] leading-[1.03] tracking-[-0.032em] text-ink [text-wrap:balance]"
              style={{ animationDelay: "0ms" }}
            >
              {hero.headlinePre}{" "}
              {hero.headlineMid}
              <span className="mark-accent">{hero.headlineAccent}</span>
              {hero.headlinePost}
            </h1>

            <p
              className="hero-rise mt-6 max-w-[46ch] text-[1.1875rem] leading-[1.55] text-muted"
              style={{ animationDelay: "120ms" }}
            >
              {hero.sub}
            </p>

            <div
              className="hero-rise mt-9 flex flex-wrap items-center gap-3"
              style={{ animationDelay: "180ms" }}
            >
              <PrimaryCTA href={authed ? ROUTES.app : ROUTES.signup} size="lg">
                {authed ? openApp : hero.primary}
              </PrimaryCTA>
              <GhostCTA href="#demo" size="lg">
                {hero.secondary}
              </GhostCTA>
            </div>

            <p
              className="hero-rise mt-6 text-[13.5px] text-faint"
              style={{ animationDelay: "240ms" }}
            >
              {hero.trust}
            </p>
          </div>

          {/* RIGHT — the product, literally. Solid + visible by default: a real
              playing clip, a second clip behind it for depth, and the reasoning
              card annotating it. The dead whitespace becomes the product shot. */}
          <div
            className="hero-rise relative mx-auto h-[440px] w-full max-w-[520px] sm:h-[512px]"
            style={{ animationDelay: "140ms" }}
          >
            {/* second clip, peeking behind top-right (static poster) */}
            <div
              data-para="-40"
              className="absolute right-[3%] top-[46px] w-[164px] rotate-[5deg] overflow-hidden rounded-[20px] bg-black shadow-pop sm:w-[188px]"
            >
              <img
                src="/clips/clip-4.webp"
                alt=""
                width={188}
                height={334}
                loading="eager"
                className="aspect-[9/16] size-full object-cover"
              />
            </div>

            {/* the hero clip — dominant, and it actually plays */}
            <div
              data-para="-88"
              className="absolute left-[2%] top-0 z-[2] w-[206px] rotate-[-3deg] overflow-hidden rounded-[24px] bg-black shadow-pop sm:w-[236px]"
            >
              <InlineClip
                src={heroClip.src!}
                poster={heroClip.poster}
                label={`Quip clip: ${heroClip.hook}`}
                className="aspect-[9/16] size-full object-cover"
              />
            </div>

            {/* the reasoning card — Quip's un-copyable artifact */}
            <aside
              data-para="34"
              data-count-scope
              className="absolute bottom-[6px] right-0 z-[4] w-[min(322px,88%)] rounded-card bg-surface p-4 shadow-pop sm:p-5"
            >
              <div className="flex items-center justify-between gap-3">
                <TypeBadge type={heroClip.type} label={heroClip.typeLabel} />
                <span className="font-mono text-[12.5px] text-muted">
                  {t("confidence").toLowerCase()}{" "}
                  <b className="font-semibold text-ok" data-count-to={heroClip.confidence}>
                    {heroClip.confidence}
                  </b>
                </span>
              </div>
              <p className="mt-3 text-[15px] font-semibold leading-snug tracking-[-0.01em] text-ink">
                {heroClip.hook}
              </p>
              <p className="mt-1.5 text-[13px] leading-snug text-muted">{heroClip.reason}</p>
              <div className="mt-3.5 h-[3px] overflow-hidden rounded-pill bg-surface-3">
                <i
                  data-count-bar
                  className="block h-full rounded-pill bg-ok"
                  style={{ width: `${heroClip.confidence}%` }}
                />
              </div>
            </aside>
          </div>
        </div>
      </Container>

      {/* signature: the conversation flowing by, moments igniting as they're caught */}
      <div className="relative mt-16 md:mt-20">
        <WaveformRiver moments={riverMoments} className="h-[96px] w-full md:h-[120px]" />
      </div>
    </section>
  );
}
