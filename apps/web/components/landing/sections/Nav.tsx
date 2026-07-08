"use client";

import { useState } from "react";
import {
  useScroll,
  useMotionValueEvent,
  AnimatePresence,
  motion,
  useReducedMotion,
} from "motion/react";
import { List, X } from "@phosphor-icons/react/dist/ssr";
import { useLocale } from "next-intl";
import { LocaleSwitcher } from "@/components/i18n/LocaleSwitcher";
import { ThemeToggle } from "@/components/ui/ThemeToggle";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent, ROUTES } from "@/lib/landingContent";
import { PrimaryCTA } from "../components/CTA";

/* Brand mark — Signal Gold rounded square + ink play triangle. Inline SVG so it
   follows the theme tokens (gold flips shade between stage and studio). */
export function Logo() {
  return (
    <a href="#top" className="flex items-center gap-2.5" aria-label="Quip home">
      <span className="grid size-7 place-items-center rounded-[9px] bg-accent shadow-[inset_0_1px_0_rgba(255,255,255,0.35),inset_0_-1px_0_rgba(0,0,0,0.12)]">
        <svg width="11" height="13" viewBox="0 0 12 14" fill="none" aria-hidden>
          <path
            d="M1.5 1.9c0-.9 1-1.5 1.8-1L11 5.8c.8.5.8 1.7 0 2.2l-7.7 4.9c-.8.5-1.8-.1-1.8-1V1.9Z"
            fill="var(--t-on-accent)"
          />
        </svg>
      </span>
      <span className="text-[19px] font-bold tracking-[-0.02em] text-ink">Quip</span>
    </a>
  );
}

/*
  Nav — starts as a quiet full-width bar over the hero, then MORPHS into a
  floating glass pill once the reader commits to the page (the fastlane move,
  done with tokens). The morph is pure CSS transitions driven by one scroll
  boolean; motion/react only drives the mobile disclosure.
*/
export function Nav({ authed = false }: { authed?: boolean }) {
  const { nav, openApp } = getLandingContent(resolveLocale(useLocale()));
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const { scrollY } = useScroll();
  const reduce = useReducedMotion();
  useMotionValueEvent(scrollY, "change", (y) => setScrolled(y > 90));

  const pill = scrolled && !open;

  return (
    <header className="fixed inset-x-0 top-0 z-50">
      <div
        className={`mx-auto transition-[max-width,margin,border-radius,background-color,box-shadow,border-color] duration-500 ease-[var(--ease-out)] ${
          pill
            ? "mt-3 max-w-[900px] rounded-pill border border-line bg-surface/90 shadow-card backdrop-blur-xl"
            : open
              ? "max-w-full border-b border-line bg-bg/85 backdrop-blur-xl"
              : "max-w-full border-b border-transparent bg-transparent"
        }`}
      >
        <nav
          className={`flex items-center justify-between gap-6 transition-[height,padding] duration-500 ease-[var(--ease-out)] ${
            pill ? "h-[54px] px-5" : "container-page h-16"
          }`}
        >
          <Logo />

          <div className="hidden items-center gap-7 md:flex">
            {nav.items.map((item) => (
              <a
                key={item.href}
                href={item.href}
                className="text-[14px] text-muted transition-colors duration-150 hover:text-ink"
              >
                {item.label}
              </a>
            ))}
          </div>

          <div className="flex items-center gap-2.5">
            <LocaleSwitcher className="hidden sm:inline-flex" />
            <ThemeToggle className="hidden sm:inline-flex" />
            {!authed && (
              <a
                href={ROUTES.login}
                className="hidden text-[14px] text-muted transition-colors duration-150 hover:text-ink sm:inline"
              >
                {nav.signIn}
              </a>
            )}
            <PrimaryCTA
              href={authed ? ROUTES.app : ROUTES.signup}
              arrow={false}
              className="h-9 px-4 text-[14px]"
            >
              {authed ? openApp : nav.tryFree}
            </PrimaryCTA>
            <button
              onClick={() => setOpen((v) => !v)}
              aria-label={open ? "Close menu" : "Open menu"}
              aria-expanded={open}
              className="grid size-9 place-items-center rounded-[8px] border border-line text-ink md:hidden"
            >
              {open ? (
                <X weight="bold" className="size-4" />
              ) : (
                <List weight="bold" className="size-4" />
              )}
            </button>
          </div>
        </nav>

        {/* mobile disclosure menu */}
        <AnimatePresence>
          {open && (
            <motion.div
              initial={reduce ? false : { height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={reduce ? undefined : { height: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: [0.2, 0.7, 0.2, 1] }}
              className="overflow-hidden md:hidden"
            >
              <div className="container-page flex flex-col gap-1 pb-4 pt-2">
                {nav.items.map((item) => (
                  <a
                    key={item.href}
                    href={item.href}
                    onClick={() => setOpen(false)}
                    className="py-2.5 text-[15px] text-muted transition-colors hover:text-ink"
                  >
                    {item.label}
                  </a>
                ))}
                {!authed && (
                  <a
                    href={ROUTES.login}
                    onClick={() => setOpen(false)}
                    className="border-t border-line py-2.5 pt-3.5 text-[15px] text-muted transition-colors hover:text-ink"
                  >
                    {nav.signIn}
                  </a>
                )}
                <div className="flex items-center gap-3 border-t border-line pt-3.5">
                  <LocaleSwitcher />
                  <ThemeToggle />
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </header>
  );
}
