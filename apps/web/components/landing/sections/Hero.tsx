import { getLocale, getTranslations } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent, ROUTES } from "@/lib/landingContent";
import { Container, TypeBadge } from "../components/primitives";
import { PrimaryCTA, GhostCTA } from "../components/CTA";
import { InlineClip } from "../components/InlineClip";
import { Reveal } from "../components/Reveal";
import { WaveformRiver, type RiverMoment } from "../fx/WaveformRiver";

/*
  Hero — the light «stage». Left: the value prop with ONE gold word. Right: a fan
  of real product clips (the middle phone actually plays) with the reasoning card
  floating over it — the thing no volume-clipper shows. Below, the signature
  WaveformRiver: an hour of conversation flowing by while the AI catches moments.
  ScrollFx drives the parallax ([data-para]) and the confidence count-up
  ([data-count-to]); with reduced motion everything renders static and complete.
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
    <section id="top" data-hero className="relative overflow-hidden pt-28 md:pt-32">
      {/* one atmospheric wash: gold breath top-left, faint peak-violet far right */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(900px 480px at 12% -10%, var(--t-accent-tint), transparent 62%)," +
            "radial-gradient(700px 420px at 88% 8%, var(--t-peak-tint), transparent 60%)",
          opacity: 0.75,
        }}
      />

      <Container className="relative">
        <div className="grid items-center gap-y-14 lg:grid-cols-[minmax(0,52fr)_minmax(0,48fr)] lg:gap-x-12">
          {/* LEFT — value prop */}
          <div className="max-w-[36rem]">
            <Reveal>
              <h1 className="text-[clamp(34px,4.1vw,46px)] font-[650] leading-[1.06] tracking-[-0.028em] text-ink [text-wrap:balance]">
                {hero.headlinePre}
                <br />
                {hero.headlineMid}
                <span className="mark-accent">{hero.headlineAccent}</span>
                {hero.headlinePost}
              </h1>
            </Reveal>
            <Reveal delay={0.06}>
              <p className="mt-6 max-w-[44ch] text-[1.0625rem] leading-relaxed text-muted">
                {hero.sub}
              </p>
            </Reveal>
            <Reveal delay={0.12}>
              <div className="mt-9 flex flex-wrap items-center gap-3">
                <PrimaryCTA href={authed ? ROUTES.app : ROUTES.signup} size="lg">
                  {authed ? openApp : hero.primary}
                </PrimaryCTA>
                <GhostCTA href="#demo" size="lg">
                  {hero.secondary}
                </GhostCTA>
              </div>
            </Reveal>
            <Reveal delay={0.18}>
              <p className="mt-6 text-[13.5px] text-faint">{hero.trust}</p>
            </Reveal>
          </div>

          {/* RIGHT — the product, literally: three real clips + the reasoning card */}
          <Reveal delay={0.1}>
            <div className="relative mx-auto h-[480px] w-full max-w-[560px] sm:h-[520px]">
              <Phone
                className="left-[2%] top-[64px] w-[176px] rotate-[-5deg] sm:w-[196px]"
                para={-42}
              >
                <img
                  src="/clips/frame-1.jpg"
                  alt=""
                  width={196}
                  height={348}
                  loading="eager"
                  className="size-full object-cover"
                />
              </Phone>
              <Phone
                className="left-[31%] top-0 z-[2] w-[210px] sm:w-[232px]"
                para={-92}
              >
                <InlineClip
                  src={heroClip.src!}
                  poster={heroClip.poster}
                  label={`Quip clip: ${heroClip.hook}`}
                  className="size-full object-cover"
                />
              </Phone>
              <Phone
                className="right-0 top-[84px] w-[176px] rotate-[4.5deg] sm:w-[196px]"
                para={-58}
              >
                <img
                  src="/clips/clip-4.webp"
                  alt=""
                  width={196}
                  height={348}
                  loading="eager"
                  className="size-full object-cover"
                />
              </Phone>

              {/* the reasoning card — Quip's un-copyable artifact */}
              <aside
                data-para="34"
                data-count-scope
                className="absolute bottom-0 left-[8%] z-[4] w-[min(340px,92%)] rounded-card bg-surface p-4 shadow-pop sm:left-[14%] sm:p-5"
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
          </Reveal>
        </div>
      </Container>

      {/* signature: the conversation flowing by, moments igniting as they're caught */}
      <div className="relative mt-14 md:mt-16">
        <WaveformRiver moments={riverMoments} className="h-[96px] w-full md:h-[120px]" />
      </div>
    </section>
  );
}

/* A phone-framed clip. Rounded, hairline ring, pop shadow; hover lifts it out
   of the fan. Parallax depth comes from ScrollFx via data-para. */
function Phone({
  children,
  className = "",
  para,
}: {
  children: React.ReactNode;
  className?: string;
  para: number;
}) {
  return (
    <div
      data-para={para}
      className={`absolute aspect-[9/16] overflow-hidden rounded-[22px] bg-[#000] shadow-pop transition-transform duration-300 ease-[var(--ease-out)] will-change-transform hover:z-[3] hover:-translate-y-1.5 hover:rotate-0 ${className}`}
    >
      {children}
    </div>
  );
}
