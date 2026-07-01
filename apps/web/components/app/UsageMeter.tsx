"use client";

import { Loader2, RotateCcw, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";
import { Button, buttonVariants } from "@/components/ui/Button";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Meter } from "@/components/ui/Meter";
import { Skeleton } from "@/components/ui/Skeleton";
import { Stat } from "@/components/ui/Stat";
import { cn } from "@/lib/cn";
import { siteConfig } from "@/lib/site";
import { deriveUsage, fmtMinutes, fmtVideos, useUsage } from "@/lib/useUsage";

// Live plan + remaining videos for the dashboard rail. Three explicit states (no silent Free
// fallback): skeleton while loading, a real error with a support line if it fails, and the
// data otherwise. Hero = TOTAL videos available now (monthly remaining + never-expiring
// PAYG); the meter below is the MONTHLY pool only. Limit numbers come from billing.py via /usage.

/** Pulsing placeholder while /usage loads — same shape as the real panel, so no layout jump. */
function MeterSkeleton() {
  const t = useTranslations("usageMeter");
  return (
    <div aria-busy="true" aria-label={t("loading")}>
      <div className="flex items-center justify-between">
        <Skeleton className="h-3 w-12" />
        <Skeleton className="h-3 w-14" />
      </div>
      <Skeleton className="mt-4 h-10 w-28" />
      <Skeleton className="mt-4 h-0.5 w-full" />
      <Skeleton className="mt-4 h-10 w-full" />
    </div>
  );
}

/** Honest failure state — the plan/limits didn't load. Says it's on us, offers retry +
 *  the support inbox, instead of silently pretending the user is on Free. */
function MeterError({ onRetry }: { onRetry: () => void }) {
  const t = useTranslations("usageMeter");
  return (
    <div role="alert">
      <div className="flex items-center gap-2 text-bad">
        <TriangleAlert className="size-4 shrink-0" aria-hidden />
        <h2 className="font-display text-base font-semibold">{t("errorTitle")}</h2>
      </div>
      <p className="mt-2 text-sm leading-relaxed text-muted">
        {t.rich("errorBody", {
          email: siteConfig.supportEmail,
          mail: (chunks) => (
            <a
              href={`mailto:${siteConfig.supportEmail}`}
              className="font-medium text-accent hover:underline"
            >
              {chunks}
            </a>
          ),
        })}
      </p>
      <Button variant="secondary" size="sm" onClick={onRetry} className="mt-4">
        <RotateCcw className="size-4" aria-hidden />
        {t("tryAgain")}
      </Button>
    </div>
  );
}

export function UsageMeter({ className }: { className?: string }) {
  const t = useTranslations("usageMeter");
  const { status, usage, reload } = useUsage();
  // Immediate feedback on click: /pricing nav can lag (cold route / slow network), and a
  // dead-looking link makes people click twice. Spinner + locked link until we leave.
  const [navigating, setNavigating] = useState(false);

  let body: React.ReactNode;
  let plan: string | null = null;

  if (status === "loading") {
    body = <MeterSkeleton />;
  } else if (status === "error" || !usage) {
    body = <MeterError onRetry={reload} />;
  } else {
    const v = deriveUsage(usage);
    plan = v.planName;
    body = (
      <>
        {/* Hero readout: the one number — how many minutes you can make right now. Meter is
            rendered manually (not via Stat's built-in `meter` prop) with a tighter mt-1: at
            this font-size the value's own line-box reserves visible space below the digits,
            so Stat's default mt-2.5 on top of that reads as a big dead gap. */}
        <Stat label={t("minutesLeft")} value={fmtMinutes(v.minutesLeft)} size="lg" tone={v.out ? "bad" : "ink"} />
        <Meter
          value={Math.max(0, 1 - v.usedPct / 100)}
          tone={v.out ? "bad" : v.near ? "warn" : "neutral"}
          className="mt-1 h-0.5"
          aria-label={t("minutesLeft")}
        />

        {/* Secondary readouts: two bordered chips, not a bare 2-col grid — wrapping inside a
            chip's own box reads as intentional, so a long label/value pair never collides
            with its neighbor the way inline label:value rows did. */}
        <div className="mt-4 grid grid-cols-2 gap-2.5">
          <div className="rounded-lg border border-line bg-surface-2 p-3">
            <Eyebrow tone="faint">{t("videosLeft")}</Eyebrow>
            {/* "~" not "≈": IBM Plex Mono is loaded with subsets:["latin"] only, which
                excludes the "≈" math glyph — it silently fell back to a different font. */}
            <p className="mt-1 font-mono text-sm tabular-nums text-ink">~{fmtVideos(v.videosLeft)}</p>
          </div>
          {v.paygVideos > 0 && (
            <div className="rounded-lg border border-line bg-surface-2 p-3">
              <Eyebrow tone="faint">{t("purchased")}</Eyebrow>
              <p className="mt-1 font-mono text-sm tabular-nums text-ink">+{fmtVideos(v.paygVideos)}</p>
              <p className="mt-0.5 font-mono text-[11px] tabular-nums text-faint">
                {fmtMinutes(v.paygMinutes)} {t("min")}
              </p>
            </div>
          )}
        </div>
        <p className="mt-3.5 text-xs leading-relaxed text-faint">{t("shortHint")}</p>

        <Link
          href="/pricing"
          onClick={() => setNavigating(true)}
          aria-busy={navigating || undefined}
          className={cn(
            buttonVariants({ variant: "secondary", size: "sm" }),
            "mt-4 w-full",
            navigating && "pointer-events-none opacity-70",
          )}
        >
          {navigating && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
          {navigating ? t("opening") : t("upgrade")}
        </Link>
      </>
    );
  }

  return (
    <section className={cn("rounded-lg border border-line bg-surface p-5", className)}>
      {/* A hairline under the card's own title row: without it, "Usage · Pro" and the hero
          Stat's own label ("Minutes left") are two same-size/same-weight eyebrow lines
          stacked with only margin between them, so they read as one run-on line rather
          than "card chrome" vs. "card content". */}
      <div className="mb-4 flex items-center justify-between border-b border-line pb-3.5">
        <Eyebrow tone="muted" as="h2">
          {t("heading")}
        </Eyebrow>
        {plan && (
          <span className="font-mono text-eyebrow uppercase tabular-nums text-faint">{plan}</span>
        )}
      </div>
      {body}
    </section>
  );
}
