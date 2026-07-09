"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import { Plus } from "@phosphor-icons/react/dist/ssr";
import { useLocale } from "next-intl";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent } from "@/lib/landingContent";
import { Container, Section } from "../components/primitives";
import { Reveal } from "../components/Reveal";

// `motion/react` (AnimatePresence + the open/close height animation) lives in
// FaqAnswer.tsx and loads as its own code-split chunk — see FaqAnswer.tsx for why
// `ssr: false` is deliberately NOT used (item 0's answer is open by default and must
// still SSR).
const FaqAnswer = dynamic(() => import("./FaqAnswer").then((mod) => mod.FaqAnswer));

export function Faq() {
  const faq = getLandingContent(resolveLocale(useLocale())).faq;
  const [open, setOpen] = useState<number | null>(0);

  return (
    <Section id="faq">
      <Container>
        <div className="grid gap-12 lg:grid-cols-[minmax(0,32fr)_minmax(0,68fr)]">
          <Reveal>
            <h2 className="text-[clamp(28px,3.6vw,44px)] font-[650] leading-[1.08] tracking-[-0.024em] text-ink">
              {faq.heading}
            </h2>
          </Reveal>

          <Reveal delay={0.05}>
            <div className="border-t border-line">
              {faq.items.map((item, i) => {
                const isOpen = open === i;
                return (
                  <div key={item.q} className="border-b border-line">
                    <button
                      onClick={() => setOpen(isOpen ? null : i)}
                      aria-expanded={isOpen}
                      className="flex w-full items-center justify-between gap-6 py-5 text-left"
                    >
                      <span className="text-[1.0625rem] font-medium tracking-[-0.01em] text-ink">{item.q}</span>
                      <Plus
                        weight="bold"
                        className={`size-4 shrink-0 text-muted transition-transform duration-300 ease-[var(--ease-snappy)] ${
                          isOpen ? "rotate-45" : ""
                        }`}
                      />
                    </button>
                    <FaqAnswer isOpen={isOpen} answer={item.a} />
                  </div>
                );
              })}
            </div>
          </Reveal>
        </div>
      </Container>
    </Section>
  );
}
