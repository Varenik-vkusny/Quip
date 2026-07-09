import { getLocale } from "next-intl/server";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";

/*
  Manifesto — the mid-page pivot. One line, pinned for a beat while the words
  come up under the scroll (ScrollFx scrubs .w opacity). Words are pre-split
  in markup (never innerHTML-split at runtime — that once mangled the gold
  span in the prototype). The dimmed initial state lives in globals.css and
  only applies when JS is active AND motion is allowed, so crawlers, no-JS,
  and reduced-motion readers always get the full line.
*/
export async function Manifesto() {
  const { manifesto } = getLandingContent(resolveLocale(await getLocale()));

  return (
    <section data-mani className="relative">
      <div className="grid min-h-[70vh] place-items-center px-6 md:min-h-screen">
        <p className="max-w-[20ch] text-center text-[clamp(36px,5.4vw,68px)] font-[650] leading-[1.06] tracking-[-0.03em] text-ink [text-wrap:balance]">
          {manifesto.words.map((word, i) => (
            <span key={i}>
              <span className={`w inline-block ${word.gold ? "mark-accent" : ""}`}>{word.t}</span>
              {i < manifesto.words.length - 1 ? " " : ""}
            </span>
          ))}
        </p>
      </div>
    </section>
  );
}
