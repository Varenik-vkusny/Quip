import { getLocale, getTranslations } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, TypeBadge } from "../components/primitives";

/*
  StudioWindow — the app as a product shot. A browser-framed window that is
  ALWAYS the dark «studio» (data-theme="dark" scopes the runtime tokens to
  this subtree — the same mechanism as the global switch), sitting on the
  light stage like a Linear/Stripe screenshot. ScrollFx gives it the 3D
  tilt-straightening entrance. Below, the three real steps of the pipeline.
  Everything inside is the actual dashboard vocabulary: create panel, usage
  meters, and two REAL clips with their reasons.
*/
export async function StudioWindow() {
  const { studio, howItWorks, heroClip, clips } = getLandingContent(
    resolveLocale(await getLocale()),
  );
  const t = await getTranslations("landingDemo");
  // clips[2] is the hook-type example: two different badge types side by side
  // read as a system, two identical ones read as a copy-paste.
  const second = clips[2];

  return (
    <section id="how-it-works" className="relative py-28 md:py-36">
      <Container>
        <div className="max-w-[46rem]">
          <h2 className="text-display-lg text-ink [text-wrap:balance]">{studio.heading}</h2>
          <p className="mt-4 max-w-[56ch] text-[1.0625rem] leading-relaxed text-muted">
            {studio.sub}
          </p>
        </div>

        <div className="mt-11" style={{ perspective: "1400px" }}>
          <div
            data-studio-window
            data-theme="dark"
            className="overflow-hidden rounded-[18px] bg-bg text-ink shadow-pop [transform-origin:50%_20%]"
          >
            {/* window chrome */}
            <div className="flex items-center gap-2 border-b border-line bg-surface px-4 py-3">
              <span className="size-2.5 rounded-pill bg-surface-3" />
              <span className="size-2.5 rounded-pill bg-surface-3" />
              <span className="size-2.5 rounded-pill bg-surface-3" />
              <span className="ml-2.5 font-mono text-[12px] text-muted">{studio.url}</span>
            </div>

            <div className="grid gap-6 p-6 sm:p-7 lg:grid-cols-[3fr_2fr]">
              {/* create panel */}
              <div>
                <div className="text-[24px] font-semibold tracking-[-0.02em]">
                  {studio.createTitle}
                </div>
                <p className="mt-1.5 max-w-[44ch] text-[14px] text-muted">{studio.createSub}</p>
                <div className="mt-5 flex gap-2.5">
                  <div className="flex flex-1 items-center gap-2.5 rounded-md border border-line-strong bg-surface px-4 py-3 text-[14px] text-muted">
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      aria-hidden
                    >
                      <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7" />
                      <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" />
                    </svg>
                    {studio.placeholder}
                  </div>
                  <span className="inline-flex items-center rounded-pill bg-accent px-5 py-3 text-[14px] font-semibold text-on-accent shadow-[inset_0_1px_0_rgba(255,255,255,0.3),inset_0_-1px_0_rgba(0,0,0,0.2)]">
                    {studio.cut}
                  </span>
                </div>
                <div className="mt-3 rounded-md border border-dashed border-line-strong px-4 py-4 text-center text-[13.5px] text-muted">
                  {studio.drop} <b className="font-semibold text-ink">{studio.dropStrong}</b>
                </div>
              </div>

              {/* usage panel */}
              <div className="rounded-lg border border-line bg-surface p-5">
                <div className="flex items-center justify-between text-[15px] font-semibold">
                  {studio.month}
                  <span className="rounded-pill border border-line-strong px-2.5 py-1 font-mono text-[11px] tracking-[0.06em] text-muted">
                    {studio.planTag}
                  </span>
                </div>
                <Meter label={studio.videos} value="0 / 1" pct={4} />
                <Meter label={studio.minutes} value="0 / 60" pct={4} />
                <span className="mt-4 inline-block text-[13.5px] font-semibold text-accent-ink">
                  {studio.upgrade} →
                </span>
              </div>

              {/* two real clips with their reasons */}
              <div className="grid gap-4 sm:grid-cols-2 lg:col-span-2">
                {[heroClip, second].map((clip) => (
                  <div
                    key={clip.id}
                    className="flex gap-3.5 rounded-lg border border-line bg-surface p-3.5"
                  >
                    <div className="w-[68px] shrink-0 overflow-hidden rounded-sm bg-[#000]">
                      <img
                        src={clip.poster}
                        alt={t("clipAlt", { caption: clip.caption ?? clip.hook })}
                        width={68}
                        height={121}
                        loading="lazy"
                        className="aspect-[9/16] size-full object-cover"
                      />
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center justify-between gap-2">
                        <TypeBadge type={clip.type} label={clip.typeLabel} />
                        <span className="font-mono text-[11.5px] text-muted">
                          <b className="font-semibold text-ok">{clip.confidence}</b>
                        </span>
                      </div>
                      <div className="mt-2 truncate text-[14px] font-semibold tracking-[-0.01em]">
                        {clip.hook}
                      </div>
                      <p className="mt-1 line-clamp-2 text-[12.5px] leading-snug text-muted">
                        {clip.reason}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* the three real steps, quiet row */}
        <div className="mt-12 grid gap-8 sm:grid-cols-3">
          {howItWorks.steps.map((step) => (
            <div key={step.n}>
              <div className="font-mono text-[12.5px] font-semibold text-accent-ink">{step.n}</div>
              <h3 className="mt-2 text-[17px] font-semibold tracking-[-0.015em] text-ink">
                {step.title}
              </h3>
              <p className="mt-1.5 max-w-[38ch] text-[14px] leading-relaxed text-muted">
                {step.body}
              </p>
            </div>
          ))}
        </div>
      </Container>
    </section>
  );
}

function Meter({ label, value, pct }: { label: string; value: string; pct: number }) {
  return (
    <div className="mt-4">
      <div className="flex items-center justify-between text-[13px] text-muted">
        <span>{label}</span>
        <b className="font-mono font-medium text-ink">{value}</b>
      </div>
      <div className="mt-1.5 h-1 overflow-hidden rounded-pill bg-surface-3">
        <i className="block h-full rounded-pill bg-accent" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
